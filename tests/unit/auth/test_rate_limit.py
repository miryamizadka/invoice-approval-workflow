"""Unit tests for services/auth/rate_limit.py (N3) - throttle_register/
throttle_login, both dual-keyed by email AND source IP. Tested through a
throwaway FastAPI app + TestClient, same idiom as
tests/unit/shared/test_rate_limiter.py's own throttle_by_user tests.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from services.auth.models import LoginRequest, RegisterRequest
from services.auth.rate_limit import _client_ip, throttle_login, throttle_register
from shared.rate_limiter import (
    InMemoryRateLimiter,
    RateLimiter,
    RateLimiterError,
    RateLimitResult,
)


class _RaisingRateLimiter:
    async def hit(
        self, *, scope: str, identity: str, limit: int, window_seconds: int
    ) -> RateLimitResult:
        raise RateLimiterError("simulated store outage")


def _build_test_app(*, limiter: RateLimiter, limit: int = 2, window_seconds: int = 60) -> FastAPI:
    app = FastAPI()
    app.state.rate_limiter = limiter

    @app.post("/auth/register")
    async def register(
        payload: RegisterRequest,
        _: None = Depends(throttle_register(limit=limit, window_seconds=window_seconds)),
    ) -> dict[str, str]:
        return {"email": payload.email}

    @app.post("/auth/login")
    async def login(
        payload: LoginRequest,
        _: None = Depends(throttle_login(limit=limit, window_seconds=window_seconds)),
    ) -> dict[str, str]:
        return {"email": payload.email}

    return app


# --- _client_ip ---------------------------------------------------------------


def test_client_ip_uses_x_forwarded_for_when_present() -> None:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"ip": _client_ip(request)}

    client = TestClient(app)
    response = client.get("/whoami", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})

    assert response.json() == {"ip": "203.0.113.5"}


def test_client_ip_falls_back_to_request_client_host_without_the_header() -> None:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"ip": _client_ip(request)}

    client = TestClient(app)
    response = client.get("/whoami")

    assert response.json()["ip"]  # TestClient always sets some client host - just non-empty


# --- throttle_register ---------------------------------------------------------


def test_throttle_register_allows_when_under_limit() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=2)
    client = TestClient(app)

    response = client.post("/auth/register", json={"email": "a@example.com", "password": "x"})

    assert response.status_code == 200


def test_throttle_register_blocks_by_email_even_from_different_source_ips() -> None:
    """Repeated attempts against the SAME target email - the classic
    account-spam pattern - must be blocked regardless of which IP each
    attempt comes from."""
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=1)
    client = TestClient(app)
    body = {"email": "victim@example.com", "password": "x"}

    client.post("/auth/register", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
    blocked = client.post("/auth/register", json=body, headers={"X-Forwarded-For": "2.2.2.2"})

    assert blocked.status_code == 429


def test_throttle_register_blocks_by_ip_even_with_a_fresh_email_each_time() -> None:
    """The primary defense for registration spam: an attacker generates a
    NEW email every attempt, so email-keying alone can't catch this -
    IP-keying must."""
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=1)
    client = TestClient(app)
    headers = {"X-Forwarded-For": "9.9.9.9"}

    client.post(
        "/auth/register", json={"email": "a@example.com", "password": "x"}, headers=headers
    )
    blocked = client.post(
        "/auth/register", json={"email": "b@example.com", "password": "x"}, headers=headers
    )

    assert blocked.status_code == 429


def test_throttle_register_429_includes_retry_after_header() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=1)
    client = TestClient(app)
    body = {"email": "a@example.com", "password": "x"}

    client.post("/auth/register", json=body)
    response = client.post("/auth/register", json=body)

    assert response.status_code == 429
    assert "Retry-After" in response.headers


# --- throttle_login -------------------------------------------------------------


def test_throttle_login_allows_when_under_limit() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=2)
    client = TestClient(app)

    response = client.post("/auth/login", json={"email": "a@example.com", "password": "x"})

    assert response.status_code == 200


def test_throttle_login_blocks_by_email_classic_brute_force() -> None:
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=1)
    client = TestClient(app)
    body = {"email": "victim@example.com", "password": "guess"}

    client.post("/auth/login", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
    blocked = client.post("/auth/login", json=body, headers={"X-Forwarded-For": "2.2.2.2"})

    assert blocked.status_code == 429


def test_throttle_login_blocks_by_ip_credential_stuffing_across_accounts() -> None:
    """One source hammering many DIFFERENT target accounts - email-keying
    alone can't see this, since each target has its own independent
    counter; IP-keying catches it."""
    app = _build_test_app(limiter=InMemoryRateLimiter(), limit=1)
    client = TestClient(app)
    headers = {"X-Forwarded-For": "9.9.9.9"}

    client.post(
        "/auth/login", json={"email": "victim1@example.com", "password": "x"}, headers=headers
    )
    blocked = client.post(
        "/auth/login", json={"email": "victim2@example.com", "password": "x"}, headers=headers
    )

    assert blocked.status_code == 429


# --- shared behavior --------------------------------------------------------


def test_fails_open_when_the_rate_limiter_is_unavailable() -> None:
    app = _build_test_app(limiter=_RaisingRateLimiter())
    client = TestClient(app)

    response = client.post("/auth/login", json={"email": "a@example.com", "password": "x"})

    assert response.status_code == 200


def test_throttle_register_validates_params_at_construction_not_lazily() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        throttle_register(limit=1, window_seconds=0)


def test_throttle_login_validates_params_at_construction_not_lazily() -> None:
    with pytest.raises(ValueError, match="limit"):
        throttle_login(limit=0, window_seconds=60)
