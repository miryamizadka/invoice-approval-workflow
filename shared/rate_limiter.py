"""Per-identity throttling (N3), fixed-window counters keyed by an
arbitrary (scope, identity) pair - "scope" distinguishes independent
budgets (e.g. "auth_login" vs "invoice_submit"), "identity" is whatever the
caller decides identifies the requester for that scope (a user's JWT `sub`,
an email pre-authentication, a source IP). Complements the gateway's
existing flat per-client-IP rate limit (M6, traefik/dynamic.yml) rather
than duplicating it - that one protects the whole system from raw traffic
volume; this one targets abuse of a specific identity/endpoint.

Deliberately fixed-window, not sliding-window/token-bucket - "good enough"
for a throttle, and the simplest mechanism that solves the actual problem
(same reasoning already used elsewhere in this project: TF-IDF over
embeddings for policy retrieval, pbkdf2 over bcrypt for password hashing).
Trade-off, accepted: a caller can get up to ~2x `limit` requests across a
window boundary (limit at the end of one window, limit again at the start
of the next) - fine for a throttle, not a hard security boundary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol

import grpc
from dapr.clients.grpc._request import TransactionalStateOperation
from fastapi import Depends, HTTPException, Request

from shared.auth import AuthenticatedUser, get_current_user
from shared.dapr_client import LazyDaprClient

logger = logging.getLogger(__name__)


class RateLimiterError(Exception):
    """A genuine backing-store failure only - never raised for a legitimate
    over-limit result (that's hit() returning allowed=False)."""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


class RateLimiter(Protocol):
    async def hit(
        self, *, scope: str, identity: str, limit: int, window_seconds: int
    ) -> RateLimitResult:
        """Atomically records one attempt in the current fixed window and
        reports whether it's still within limit. Never raises on a normal
        over-limit - only RateLimiterError on a genuine store failure, and
        ValueError if `limit`/`window_seconds` themselves are invalid
        (fail-fast at the call site, not a silent ZeroDivisionError buried
        in the bucket computation)."""
        ...


def validate_rate_limit_params(limit: int, window_seconds: int) -> None:
    """Exported (not just used internally by hit()) so the FastAPI
    dependency factories below - and services/auth/rate_limit.py's own -
    can fail fast at route-decoration time (service startup) on a
    misconfigured limit/window, rather than lazily on the first request
    that happens to reach hit() and hit the same check there."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if window_seconds < 1:
        raise ValueError(f"window_seconds must be >= 1, got {window_seconds}")


def _bucket_and_retry_after(now: float, window_seconds: int) -> tuple[int, int]:
    bucket = int(now // window_seconds)
    window_end = (bucket + 1) * window_seconds
    return bucket, int(window_end - now)


def _key(scope: str, identity: str, bucket: int) -> str:
    # Bucket is the LAST segment, deliberately - keeps a future
    # `SCAN ratelimit:{scope}:{identity}:*` prefix-scan meaningful without
    # the bucket number splitting it up.
    return f"ratelimit:{scope}:{identity}:{bucket}"


class InMemoryRateLimiter:
    """Test double - a plain dict, safe under asyncio with zero locking
    (no `await` between read-check-write, same reasoning that makes every
    other in-memory fake in this codebase safe under cooperative
    scheduling)."""

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.time
        self._counts: dict[str, int] = {}

    async def hit(
        self, *, scope: str, identity: str, limit: int, window_seconds: int
    ) -> RateLimitResult:
        validate_rate_limit_params(limit, window_seconds)
        current_time = self._now()
        bucket, retry_after_seconds = _bucket_and_retry_after(current_time, window_seconds)
        key = _key(scope, identity, bucket)
        current = self._counts.get(key, 0)
        if current >= limit:
            return RateLimitResult(allowed=False, retry_after_seconds=retry_after_seconds)
        self._counts[key] = current + 1
        return RateLimitResult(allowed=True, retry_after_seconds=retry_after_seconds)


class _StateResponse(Protocol):
    @property
    def data(self) -> bytes | str: ...
    @property
    def etag(self) -> str: ...


class _DaprStateClient(Protocol):
    async def get_state(self, store_name: str, key: str) -> _StateResponse: ...

    async def execute_state_transaction(
        self, store_name: str, operations: list[Any]
    ) -> object: ...


_STORE_NAME = "statestore"
_MAX_RETRIES = 5  # a defensive ceiling for genuine concurrent contention on
# the same bucket key - normal operation should very rarely need more than
# 1-2 retries even under load; same order of magnitude as
# DaprStateBudgetRepository.reserve()'s own retry loop (INV-1014).
_RETRY_DELAY_SECONDS = 0.01  # small, fixed - avoids a retry storm under real contention.


class DaprStateRateLimiter:
    """Combines DaprStateBudgetRepository.reserve()'s conditional-update
    retry loop with ensure_seeded()'s create-if-absent ETag-when-empty
    pattern into one loop: a bucket's first hit is a create, every
    subsequent hit in the same window is a conditional update - both cases
    go through the same read -> check -> conditional-write cycle.

    Known limitation, verified live (not assumed): no TTL on these keys.
    Dapr's Redis state store silently ignores per-operation
    metadata={"ttlInSeconds": ...} on execute_state_transaction() - writing
    a key this way and checking `redis-cli TTL` confirmed no expiry was
    set (-1), even though the identical metadata on a plain save_state()
    call does apply one. Switching to save_state() to get TTL was
    considered and rejected: its ETag-conflict path raises
    DaprInternalError (a plain Exception, not grpc.RpcError - the same
    reason DaprStateBudgetRepository never uses it for conditional writes
    either), and its etag-conflict behavior showed unclear/untrusted
    results under direct testing here. Correctness of the counter itself
    (the actual throttling guarantee) was kept over self-cleaning storage -
    rate-limit keys accumulate in Redis without auto-expiry; a real,
    accepted limitation for now, not a silent gap. Revisit only if this
    becomes an actual operational problem.
    """

    def __init__(
        self,
        *,
        client: _DaprStateClient | None = None,
        factory: Callable[[], _DaprStateClient] | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprStateClient](client, factory=factory)
        self._now = now or time.time

    async def hit(
        self, *, scope: str, identity: str, limit: int, window_seconds: int
    ) -> RateLimitResult:
        validate_rate_limit_params(limit, window_seconds)
        current_time = self._now()
        bucket, retry_after_seconds = _bucket_and_retry_after(current_time, window_seconds)
        key = _key(scope, identity, bucket)
        for _ in range(_MAX_RETRIES):
            try:
                response = await self._dapr.get().get_state(_STORE_NAME, key)
            except grpc.RpcError as exc:
                raise RateLimiterError(f"Failed to read rate limit counter {key!r}: {exc}") from exc
            try:
                current = int(response.data) if response.data else 0
            except ValueError as exc:
                raise RateLimiterError(
                    f"Corrupted rate limit counter data for {key!r}: {exc}"
                ) from exc
            if current >= limit:
                return RateLimitResult(allowed=False, retry_after_seconds=retry_after_seconds)
            operation = TransactionalStateOperation(
                key=key,
                data=str(current + 1),
                etag=response.etag,
                # No TTL metadata here - see this class's own docstring for
                # why (verified live: Dapr's Redis state store silently
                # ignores it on transactional writes).
            )
            try:
                await self._dapr.get().execute_state_transaction(_STORE_NAME, [operation])
                return RateLimitResult(allowed=True, retry_after_seconds=retry_after_seconds)
            except grpc.RpcError:
                # etag was stale (concurrent writer) - re-read and retry
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        raise RateLimiterError(f"Exceeded {_MAX_RETRIES} retries incrementing {key!r}")


def throttle_by_user(
    *, scope: str, limit: int, window_seconds: int
) -> Callable[..., Coroutine[Any, Any, AuthenticatedUser]]:
    """FastAPI dependency-factory - the same "wraps another dependency"
    shape as shared/auth.py's require_role(): depends on get_current_user
    itself, so a route's existing `Depends(get_current_user)` param can be
    swapped for `Depends(throttle_by_user(...))` directly, no other change
    needed. Validated once here, at route-decoration time (service
    startup), not per-request.

    Reads the limiter instance off `request.app.state.rate_limiter` -
    each service's create_app() sets it, mirroring the existing
    app.state.<x>_service convention."""
    validate_rate_limit_params(limit, window_seconds)

    async def _check(
        request: Request, user: AuthenticatedUser = Depends(get_current_user)
    ) -> AuthenticatedUser:
        limiter: RateLimiter = request.app.state.rate_limiter
        try:
            result = await limiter.hit(
                scope=scope, identity=user.sub, limit=limit, window_seconds=window_seconds
            )
        except RateLimiterError:
            # Fail OPEN: a limiter-store outage must never block real
            # submissions - N3 throttling is defense-in-depth, not a hard
            # gate the rest of the system depends on for correctness.
            logger.warning(
                "rate_limiter_unavailable_failing_open",
                extra={"scope": scope, "identity": user.sub},
            )
            return user
        if not result.allowed:
            logger.warning("rate_limit_exceeded", extra={"scope": scope, "identity": user.sub})
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(result.retry_after_seconds)},
            )
        return user

    return _check
