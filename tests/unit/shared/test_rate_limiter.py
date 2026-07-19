"""Unit tests for shared/rate_limiter.py: RateLimitResult, InMemoryRateLimiter
(pure), DaprStateRateLimiter (Dapr-backed, fake client - mirrors
tests/unit/payment/test_dapr_state_repository.py's own
_FakeStateResponse/_FakeDaprStateClient shape), and throttle_by_user (the
FastAPI dependency, tested through a throwaway app + TestClient - same
idiom tests/unit/shared/test_auth.py uses for get_current_user/require_role).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import grpc
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from shared.auth import AuthenticatedUser, Role, create_access_token
from shared.rate_limiter import (
    DaprStateRateLimiter,
    InMemoryRateLimiter,
    RateLimiter,
    RateLimiterError,
    RateLimitResult,
    throttle_by_user,
)


def _clock(times: list[float]) -> Callable[[], float]:
    """Returns successive values from `times` on each call - lets a test
    control exactly what `now()` returns per hit() call, without real
    sleeping (mirrors shared/bulkhead.py's own test style of controlling
    time via asyncio.sleep durations, but here via direct injection since
    RateLimiter's whole point is a clock-derived bucket key)."""
    iterator = iter(times)
    return lambda: next(iterator)


async def test_hit_allows_when_under_limit() -> None:
    limiter = InMemoryRateLimiter(now=_clock([100.0]))

    result = await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)

    assert result.allowed is True


async def test_hit_blocks_once_limit_is_reached() -> None:
    limiter = InMemoryRateLimiter(now=_clock([100.0, 100.0, 100.0]))

    first = await limiter.hit(scope="test", identity="alice", limit=2, window_seconds=60)
    second = await limiter.hit(scope="test", identity="alice", limit=2, window_seconds=60)
    third = await limiter.hit(scope="test", identity="alice", limit=2, window_seconds=60)

    assert (first.allowed, second.allowed, third.allowed) == (True, True, False)


async def test_hit_isolates_different_identities_under_the_same_scope() -> None:
    limiter = InMemoryRateLimiter(now=_clock([100.0, 100.0, 100.0]))

    await limiter.hit(scope="test", identity="alice", limit=1, window_seconds=60)
    alice_second = await limiter.hit(scope="test", identity="alice", limit=1, window_seconds=60)
    bob_first = await limiter.hit(scope="test", identity="bob", limit=1, window_seconds=60)

    assert alice_second.allowed is False
    assert bob_first.allowed is True


async def test_hit_isolates_different_scopes_for_the_same_identity() -> None:
    limiter = InMemoryRateLimiter(now=_clock([100.0, 100.0]))

    login = await limiter.hit(scope="auth_login", identity="alice", limit=1, window_seconds=60)
    register = await limiter.hit(
        scope="auth_register", identity="alice", limit=1, window_seconds=60
    )

    assert login.allowed is True
    assert register.allowed is True


async def test_hit_allows_again_once_the_window_rolls_over() -> None:
    # window_seconds=60: bucket 1 is [60,120), bucket 2 is [120,180) - 100.0
    # and 125.0 fall in different buckets, so the counter resets between them.
    limiter = InMemoryRateLimiter(now=_clock([100.0, 100.0, 125.0]))

    await limiter.hit(scope="test", identity="alice", limit=1, window_seconds=60)
    blocked_same_window = await limiter.hit(
        scope="test", identity="alice", limit=1, window_seconds=60
    )
    allowed_next_window = await limiter.hit(
        scope="test", identity="alice", limit=1, window_seconds=60
    )

    assert blocked_same_window.allowed is False
    assert allowed_next_window.allowed is True


async def test_hit_retry_after_seconds_reflects_time_left_in_the_current_window() -> None:
    # now=125.0, window_seconds=60 -> current bucket [120,180) ends at 180 -> 55s left.
    limiter = InMemoryRateLimiter(now=_clock([125.0]))

    result = await limiter.hit(scope="test", identity="alice", limit=1, window_seconds=60)

    assert result.retry_after_seconds == 55


async def test_hit_raises_value_error_when_limit_is_below_one() -> None:
    limiter = InMemoryRateLimiter()

    with pytest.raises(ValueError, match="limit"):
        await limiter.hit(scope="test", identity="alice", limit=0, window_seconds=60)


async def test_hit_raises_value_error_when_window_seconds_is_below_one() -> None:
    """Also closes a real ZeroDivisionError risk in the bucket computation
    (`now // window_seconds`) - not just a generic validation nicety."""
    limiter = InMemoryRateLimiter()

    with pytest.raises(ValueError, match="window_seconds"):
        await limiter.hit(scope="test", identity="alice", limit=1, window_seconds=0)


# --- DaprStateRateLimiter ----------------------------------------------------


class _FakeStateResponse:
    def __init__(self, data: bytes, etag: str = "") -> None:
        self.data = data
        self.etag = etag


class _FakeDaprStateClient:
    """Mirrors tests/unit/payment/test_dapr_state_repository.py's own fake -
    a per-key etag, incrementing on every successful write, close enough to
    real Redis-backed Dapr state to exercise the read-check-write loop."""

    def __init__(self, *, fail_transactions: int = 0) -> None:
        self.store: dict[str, str] = {}
        self.etags: dict[str, str] = {}
        self.metadata_by_key: dict[str, dict[str, str] | None] = {}
        self._etag_counter = 0
        self._fail_transactions = fail_transactions
        self.get_state_calls = 0
        self.transaction_calls = 0

    async def get_state(self, store_name: str, key: str) -> _FakeStateResponse:
        self.get_state_calls += 1
        data = self.store.get(key, "")
        return _FakeStateResponse(data.encode("utf-8"), self.etags.get(key, ""))

    async def execute_state_transaction(self, store_name: str, operations: list[Any]) -> None:
        self.transaction_calls += 1
        if self.transaction_calls <= self._fail_transactions:
            raise grpc.RpcError()
        for op in operations:
            if op.etag is not None and op.etag != self.etags.get(op.key, ""):
                raise grpc.RpcError()
        for op in operations:
            data = op.data if isinstance(op.data, str) else op.data.decode("utf-8")
            self.store[op.key] = data
            self.metadata_by_key[op.key] = op.metadata
            self._etag_counter += 1
            self.etags[op.key] = str(self._etag_counter)


class _RaisingOnGetDaprStateClient:
    async def get_state(self, store_name: str, key: str) -> _FakeStateResponse:
        raise grpc.RpcError()

    async def execute_state_transaction(self, store_name: str, operations: list[Any]) -> None:
        raise AssertionError("should never be reached")


async def test_dapr_hit_allows_and_writes_incremented_count_on_first_call() -> None:
    fake = _FakeDaprStateClient()
    limiter = DaprStateRateLimiter(client=fake, now=_clock([100.0]))

    result = await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)

    assert result.allowed is True
    assert fake.store["ratelimit:test:alice:1"] == "1"


async def test_dapr_hit_increments_an_existing_count() -> None:
    fake = _FakeDaprStateClient()
    limiter = DaprStateRateLimiter(client=fake, now=_clock([100.0, 100.0]))

    await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)
    await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)

    assert fake.store["ratelimit:test:alice:1"] == "2"


async def test_dapr_hit_blocks_at_limit_without_writing_again() -> None:
    fake = _FakeDaprStateClient()
    limiter = DaprStateRateLimiter(client=fake, now=_clock([100.0, 100.0]))

    await limiter.hit(scope="test", identity="alice", limit=1, window_seconds=60)
    transactions_after_first = fake.transaction_calls
    result = await limiter.hit(scope="test", identity="alice", limit=1, window_seconds=60)

    assert result.allowed is False
    assert fake.transaction_calls == transactions_after_first  # no write attempted


async def test_dapr_hit_sends_no_ttl_metadata_on_write() -> None:
    """Regression guard, not a feature test: a `metadata={"ttlInSeconds":
    ...}` was tried and removed after live testing (redis-cli TTL) showed
    Dapr's Redis state store silently ignores it on transactional writes -
    see DaprStateRateLimiter's own docstring. If a future change
    re-introduces this metadata without re-verifying it live, this test
    fails as a reminder, not because sending it would be actively wrong."""
    fake = _FakeDaprStateClient()
    limiter = DaprStateRateLimiter(client=fake, now=_clock([100.0]))

    await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)

    # TransactionalStateOperation normalizes an omitted metadata= to {},
    # not None - falsy either way, which is what matters here.
    assert not fake.metadata_by_key["ratelimit:test:alice:1"]


async def test_dapr_hit_retries_on_a_transient_etag_conflict_and_succeeds() -> None:
    fake = _FakeDaprStateClient(fail_transactions=1)
    limiter = DaprStateRateLimiter(client=fake, now=_clock([100.0, 100.0]))

    result = await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)

    assert result.allowed is True
    assert fake.transaction_calls == 2


async def test_dapr_hit_raises_rate_limiter_error_after_exhausting_retries() -> None:
    fake = _FakeDaprStateClient(fail_transactions=999)
    limiter = DaprStateRateLimiter(client=fake, now=_clock([100.0] * 10))

    with pytest.raises(RateLimiterError, match="retries"):
        await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)


async def test_dapr_hit_raises_rate_limiter_error_when_get_state_fails() -> None:
    limiter = DaprStateRateLimiter(client=_RaisingOnGetDaprStateClient(), now=_clock([100.0]))

    with pytest.raises(RateLimiterError):
        await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)


async def test_dapr_hit_raises_rate_limiter_error_on_corrupted_counter_data() -> None:
    fake = _FakeDaprStateClient()
    fake.store["ratelimit:test:alice:1"] = "not-a-number"
    limiter = DaprStateRateLimiter(client=fake, now=_clock([100.0]))

    with pytest.raises(RateLimiterError, match="[Cc]orrupt"):
        await limiter.hit(scope="test", identity="alice", limit=3, window_seconds=60)


async def test_dapr_hit_raises_value_error_for_invalid_limit_before_touching_dapr() -> None:
    limiter = DaprStateRateLimiter(client=_RaisingOnGetDaprStateClient())

    with pytest.raises(ValueError, match="limit"):
        await limiter.hit(scope="test", identity="alice", limit=0, window_seconds=60)


# --- throttle_by_user (FastAPI dependency) -----------------------------------

_SECRET = "test-secret"


class _RaisingRateLimiter:
    """Simulates a backing-store outage - every hit() call fails."""

    async def hit(
        self, *, scope: str, identity: str, limit: int, window_seconds: int
    ) -> RateLimitResult:
        raise RateLimiterError("simulated store outage")


def _build_test_app(*, limiter: RateLimiter, limit: int = 2, window_seconds: int = 60) -> FastAPI:
    app = FastAPI()
    app.state.rate_limiter = limiter

    @app.post("/submit")
    async def submit(
        user: AuthenticatedUser = Depends(
            throttle_by_user(scope="test_submit", limit=limit, window_seconds=window_seconds)
        ),
    ) -> dict[str, str]:
        return {"sub": user.sub}

    return app


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", _SECRET)


def _token(subject: str = "alice@example.com") -> str:
    return create_access_token(subject=subject, role=Role.SUBMITTER, secret=_SECRET)


def test_throttle_by_user_allows_when_under_limit() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=2)
    client = TestClient(app)

    response = client.post("/submit", headers={"Authorization": f"Bearer {_token()}"})

    assert response.status_code == 200


def test_throttle_by_user_returns_429_once_the_limit_is_reached() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=2)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_token()}"}

    client.post("/submit", headers=headers)
    client.post("/submit", headers=headers)
    third = client.post("/submit", headers=headers)

    assert third.status_code == 429


def test_throttle_by_user_429_includes_retry_after_header() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=1)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_token()}"}

    client.post("/submit", headers=headers)
    response = client.post("/submit", headers=headers)

    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_throttle_by_user_isolates_different_authenticated_identities() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=1)
    client = TestClient(app)

    alice = client.post(
        "/submit", headers={"Authorization": f"Bearer {_token('alice@example.com')}"}
    )
    bob = client.post("/submit", headers={"Authorization": f"Bearer {_token('bob@example.com')}"})

    assert alice.status_code == 200
    assert bob.status_code == 200


def test_throttle_by_user_requires_authentication() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter())
    client = TestClient(app)

    response = client.post("/submit")

    assert response.status_code == 401


def test_throttle_by_user_fails_open_when_the_rate_limiter_is_unavailable() -> None:
    app = _build_test_app(limiter=_RaisingRateLimiter())
    client = TestClient(app)

    response = client.post("/submit", headers={"Authorization": f"Bearer {_token()}"})

    assert response.status_code == 200


def test_throttle_by_user_validates_params_at_construction_not_lazily() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        throttle_by_user(scope="test", limit=1, window_seconds=0)
