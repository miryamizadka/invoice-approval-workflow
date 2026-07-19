"""Auth-specific throttling (N3) - throttle_register/throttle_login, both
dual-keyed by BOTH the target email AND the source IP, for two different
reasons:

- Register: an attacker generates a FRESH email every attempt, so an
  email-keyed limit alone is nearly useless against registration spam -
  the email isn't fixed, the attacker chooses it. IP-keying is the
  PRIMARY defense here.
- Login: email-keying catches repeated attempts against ONE account
  (classic brute-force); IP-keying additionally catches credential-
  stuffing across MANY different target accounts from one source, which
  email-keying alone can't see (each target has its own independent
  counter).

Lives here, not in shared/rate_limiter.py, because the FastAPI-idiomatic
way to access the parsed request body from within a dependency is to type
the dependency's own parameter as the exact concrete Pydantic model the
route itself uses (RegisterRequest/LoginRequest) - both live in
services/auth/models.py, and shared/ never imports from services/* (a
real, verified boundary - zero existing imports). The underlying
mechanism (RateLimiter, hit()) is still fully shared - only these two
thin, model-typed closures are service-local.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request

from services.auth.models import LoginRequest, RegisterRequest
from shared.rate_limiter import RateLimiter, RateLimiterError, validate_rate_limit_params

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """Every inbound request arrives from Traefik's own container, not the
    original client (M6: the gateway is the sole external entry point) -
    trusting its X-Forwarded-For here is safe specifically because there's
    no untrusted proxy chain in front of it to spoof this header from.
    Falls back to request.client.host if the header is somehow absent."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _throttle(
    request: Request, *, scope: str, email: str, limit: int, window_seconds: int
) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    client_ip = _client_ip(request)
    try:
        by_email = await limiter.hit(
            scope=f"{scope}_email", identity=email, limit=limit, window_seconds=window_seconds
        )
        by_ip = await limiter.hit(
            scope=f"{scope}_ip", identity=client_ip, limit=limit, window_seconds=window_seconds
        )
    except RateLimiterError:
        # Fail OPEN - see shared.rate_limiter.throttle_by_user's identical
        # posture: a limiter-store outage must never block real
        # login/register traffic.
        logger.warning("rate_limiter_unavailable_failing_open", extra={"scope": scope})
        return
    if not by_email.allowed or not by_ip.allowed:
        blocked = by_email if not by_email.allowed else by_ip
        logger.warning(
            "rate_limit_exceeded", extra={"scope": scope, "email": email, "client_ip": client_ip}
        )
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(blocked.retry_after_seconds)},
        )


def throttle_register(
    *, limit: int, window_seconds: int
) -> Callable[..., Coroutine[Any, Any, None]]:
    validate_rate_limit_params(limit, window_seconds)

    async def _check(payload: RegisterRequest, request: Request) -> None:
        await _throttle(
            request,
            scope="auth_register",
            email=payload.email,
            limit=limit,
            window_seconds=window_seconds,
        )

    return _check


def throttle_login(*, limit: int, window_seconds: int) -> Callable[..., Coroutine[Any, Any, None]]:
    validate_rate_limit_params(limit, window_seconds)

    async def _check(payload: LoginRequest, request: Request) -> None:
        await _throttle(
            request,
            scope="auth_login",
            email=payload.email,
            limit=limit,
            window_seconds=window_seconds,
        )

    return _check
