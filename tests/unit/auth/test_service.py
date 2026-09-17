"""Tests for services/auth/service.py's AuthService - register()/login()
business logic, tested against a fake in-memory UserRepository (same style
as other services' Manager-layer tests using an InMemory* fake).
"""

from __future__ import annotations

import jwt
import pytest

from services.auth.hashing import hash_password
from services.auth.models import User
from services.auth.repository import (
    InMemoryUserRepository,
    InvalidCredentialsError,
    UserAlreadyExistsError,
)
from services.auth.service import AuthService, build_auth_service
from shared.auth import Role, decode_token

SECRET = "test-jwt-secret-for-service-tests"


def _service() -> tuple[AuthService, InMemoryUserRepository]:
    repository = InMemoryUserRepository()
    return AuthService(repository, jwt_secret=SECRET), repository


# --- register() --------------------------------------------------------------


async def test_register_creates_a_submitter_with_hashed_password() -> None:
    service, repository = _service()

    response = await service.register("alice@example.com", "hunter2")

    assert response.email == "alice@example.com"
    assert response.role == Role.SUBMITTER
    stored = await repository.get_by_email("alice@example.com")
    assert stored is not None
    assert stored.password_hash != "hunter2"  # never plaintext
    assert stored.role == Role.SUBMITTER


async def test_register_duplicate_email_raises() -> None:
    service, _ = _service()
    await service.register("alice@example.com", "hunter2")

    with pytest.raises(UserAlreadyExistsError):
        await service.register("alice@example.com", "different-password")


# --- login() -------------------------------------------------------------


async def test_login_with_correct_credentials_returns_a_valid_token() -> None:
    service, _ = _service()
    await service.register("alice@example.com", "hunter2")

    response = await service.login("alice@example.com", "hunter2")

    user = decode_token(response.access_token, secret=SECRET)
    assert user.sub == "alice@example.com"
    assert user.role == Role.SUBMITTER
    assert response.token_type == "bearer"


async def test_login_with_wrong_password_raises_invalid_credentials() -> None:
    service, _ = _service()
    await service.register("alice@example.com", "hunter2")

    with pytest.raises(InvalidCredentialsError):
        await service.login("alice@example.com", "wrong-password")


async def test_login_with_unknown_email_raises_the_same_error_as_wrong_password() -> None:
    """Anti-enumeration: an unknown email and a wrong password must be
    indistinguishable to the caller."""
    service, _ = _service()

    with pytest.raises(InvalidCredentialsError):
        await service.login("nobody@example.com", "irrelevant")


async def test_login_issues_a_token_with_the_seeded_role_not_always_submitter() -> None:
    service, repository = _service()
    password_hash, salt = hash_password("approver-pass")
    await repository.save(
        User(
            email="approver@example.com",
            password_hash=password_hash,
            salt=salt,
            role=Role.APPROVER,
        )
    )

    response = await service.login("approver@example.com", "approver-pass")

    user = decode_token(response.access_token, secret=SECRET)
    assert user.role == Role.APPROVER


# --- build_auth_service() - must never hard-fail at construction time ------


def test_build_auth_service_does_not_raise_when_jwt_secret_env_var_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction must never hard-fail on missing config - the same
    resilience posture every other service in this project already follows
    (e.g. Decision's LLM provider resolution never fails at import time).
    A module-level `app = create_app()` singleton means this runs at
    import time - raising here would break even importing the module."""
    monkeypatch.delenv("JWT_SECRET", raising=False)

    build_auth_service(InMemoryUserRepository())  # must not raise


async def test_login_raises_clearly_when_jwt_secret_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    service = build_auth_service(InMemoryUserRepository())
    await service.register("alice@example.com", "hunter2")

    with pytest.raises(RuntimeError):
        await service.login("alice@example.com", "hunter2")


async def test_build_auth_service_reads_jwt_expiry_hours_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_EXPIRY_HOURS", "1")
    service = build_auth_service(InMemoryUserRepository(), jwt_secret=SECRET)
    await service.register("alice@example.com", "hunter2")

    response = await service.login("alice@example.com", "hunter2")

    claims = jwt.decode(response.access_token, SECRET, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 3600
