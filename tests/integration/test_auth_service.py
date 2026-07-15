"""Integration tests for the Auth Service HTTP API.

Exercises the full chain: HTTP -> FastAPI -> AuthService - with an injected
InMemoryUserRepository, no real Dapr.

Auth has genuine async startup work (demo user seeding via the lifespan
hook) - same as test_payment_service.py, this uses `with TestClient(app) as
client:` so the lifespan actually runs (a bare `TestClient(app)` never
triggers ASGI lifespan startup).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.auth.app import create_app
from services.auth.repository import InMemoryUserRepository
from shared.auth import Role, decode_token

JWT_SECRET = "test-secret-for-auth-integration"


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)


def test_health() -> None:
    with TestClient(create_app(repository=InMemoryUserRepository())) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_register_then_login_round_trip() -> None:
    with TestClient(create_app(repository=InMemoryUserRepository())) as client:
        register_response = client.post(
            "/auth/register", json={"email": "alice@example.com", "password": "hunter2"}
        )
        assert register_response.status_code == 201
        assert register_response.json() == {"email": "alice@example.com", "role": "submitter"}

        login_response = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "hunter2"}
        )

    assert login_response.status_code == 200
    body = login_response.json()
    assert body["token_type"] == "bearer"
    user = decode_token(body["access_token"], secret=JWT_SECRET)
    assert user.sub == "alice@example.com"
    assert user.role == Role.SUBMITTER


def test_register_duplicate_email_returns_409() -> None:
    with TestClient(create_app(repository=InMemoryUserRepository())) as client:
        client.post("/auth/register", json={"email": "alice@example.com", "password": "hunter2"})

        response = client.post(
            "/auth/register", json={"email": "alice@example.com", "password": "different"}
        )

    assert response.status_code == 409


def test_login_with_wrong_password_returns_401() -> None:
    with TestClient(create_app(repository=InMemoryUserRepository())) as client:
        client.post("/auth/register", json={"email": "alice@example.com", "password": "hunter2"})

        response = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": "wrong"}
        )

    assert response.status_code == 401


def test_login_with_unknown_email_returns_401() -> None:
    with TestClient(create_app(repository=InMemoryUserRepository())) as client:
        response = client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "irrelevant"}
        )

    assert response.status_code == 401


def test_demo_accounts_are_seeded_and_can_log_in() -> None:
    """Proves the lifespan hook actually ran and seeded the demo Approver/
    Admin accounts from services/auth/demo_users.json."""
    with TestClient(create_app(repository=InMemoryUserRepository())) as client:
        response = client.post(
            "/auth/login", json={"email": "approver@example.com", "password": "ApproverDemo123!"}
        )

    assert response.status_code == 200
    user = decode_token(response.json()["access_token"], secret=JWT_SECRET)
    assert user.role == Role.APPROVER
