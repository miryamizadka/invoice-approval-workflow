"""Tests for shared/auth.py (N1) - JWT issuance/verification + the
get_current_user/require_role FastAPI dependencies every protected route
across the 6 existing services wires into.

get_current_user/require_role are tested through a tiny throwaway FastAPI
app + TestClient (not a hand-constructed Starlette Request) - same idiom
this project's own integration tests already use elsewhere.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from shared.auth import (
    ALGORITHM,
    AuthenticatedUser,
    Role,
    TokenError,
    create_access_token,
    decode_token,
    get_current_user,
    require_role,
)

SECRET = "test-secret"


# --- create_access_token / decode_token -------------------------------------


def test_round_trip_recovers_subject_and_role() -> None:
    token = create_access_token(subject="alice@example.com", role=Role.APPROVER, secret=SECRET)

    user = decode_token(token, secret=SECRET)

    assert user.sub == "alice@example.com"
    assert user.role == Role.APPROVER


def test_decode_raises_on_expired_token() -> None:
    token = create_access_token(
        subject="alice@example.com", role=Role.SUBMITTER, secret=SECRET, expires_hours=-1
    )

    with pytest.raises(TokenError):
        decode_token(token, secret=SECRET)


def test_decode_raises_on_wrong_secret() -> None:
    token = create_access_token(subject="alice@example.com", role=Role.SUBMITTER, secret=SECRET)

    with pytest.raises(TokenError):
        decode_token(token, secret="a-different-secret")


def test_decode_raises_on_garbage_string() -> None:
    with pytest.raises(TokenError):
        decode_token("not-a-jwt-at-all", secret=SECRET)


def test_decode_raises_on_missing_claims() -> None:
    token = jwt.encode({"iat": int(time.time())}, SECRET, algorithm=ALGORITHM)

    with pytest.raises(TokenError):
        decode_token(token, secret=SECRET)


def test_decode_raises_on_unknown_role() -> None:
    token = jwt.encode(
        {"sub": "alice@example.com", "role": "superuser", "iat": int(time.time())},
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(TokenError):
        decode_token(token, secret=SECRET)


def test_decode_rejects_a_token_signed_with_a_different_algorithm() -> None:
    """Algorithm-confusion guard: decode_token must pin algorithms=["HS256"]
    explicitly, not let PyJWT infer it from the token's own header - a token
    signed HS512 (or "none") must never be accepted just because the
    attacker controls the alg header."""
    token = jwt.encode(
        {"sub": "alice@example.com", "role": "submitter", "iat": int(time.time())},
        SECRET,
        algorithm="HS512",
    )

    with pytest.raises(TokenError):
        decode_token(token, secret=SECRET)


def test_decode_rejects_an_unsigned_none_algorithm_token() -> None:
    # key="" (not None) - a plain valid str under any PyJWT stub version, so
    # this doesn't need a type: ignore that could itself become an "unused
    # ignore" error under a different stub version (as happened in CI).
    # algorithm="none" means PyJWT never actually uses the key value anyway.
    token = jwt.encode(
        {"sub": "alice@example.com", "role": "admin", "iat": int(time.time())},
        key="",
        algorithm="none",
    )

    with pytest.raises(TokenError):
        decode_token(token, secret=SECRET)


# --- get_current_user / require_role, via a real FastAPI app ---------------


def _build_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: AuthenticatedUser = Depends(get_current_user)) -> dict[str, str]:
        return {"sub": user.sub, "role": user.role.name}

    @app.get("/approver-only")
    async def approver_only(
        user: AuthenticatedUser = Depends(require_role(Role.APPROVER)),
    ) -> dict[str, str]:
        return {"sub": user.sub}

    return app


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", SECRET)


def test_get_current_user_rejects_missing_header() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/whoami")

    assert response.status_code == 401


def test_get_current_user_rejects_header_without_bearer_prefix() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/whoami", headers={"Authorization": "not-bearer xyz"})

    assert response.status_code == 401


def test_get_current_user_rejects_invalid_token() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/whoami", headers={"Authorization": "Bearer garbage"})

    assert response.status_code == 401


def test_get_current_user_accepts_valid_token() -> None:
    client = TestClient(_build_test_app())
    token = create_access_token(subject="bob@example.com", role=Role.SUBMITTER, secret=SECRET)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"sub": "bob@example.com", "role": "SUBMITTER"}


@pytest.mark.parametrize(
    ("role", "expected_status"),
    [
        (Role.SUBMITTER, 403),
        (Role.APPROVER, 200),
        (Role.ADMIN, 200),
    ],
)
def test_require_role_gate(role: Role, expected_status: int) -> None:
    client = TestClient(_build_test_app())
    token = create_access_token(subject="carol@example.com", role=role, secret=SECRET)

    response = client.get("/approver-only", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == expected_status


def test_require_role_rejects_with_no_token_at_all() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/approver-only")

    assert response.status_code == 401


# --- app.state.jwt_secret (M5/N1: Dapr-sourced secret) takes precedence ----


def test_get_current_user_accepts_token_signed_with_app_state_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app.state.jwt_secret is how each service's lifespan hands over a
    Dapr-resolved secret (see shared/jwt_secret_loader.py) - it must win
    over the JWT_SECRET env var, not just be an equally-valid alternative."""
    monkeypatch.setenv("JWT_SECRET", "env-secret-should-not-be-used")
    app = _build_test_app()
    app.state.jwt_secret = "state-secret"
    client = TestClient(app)
    token = create_access_token(
        subject="dave@example.com", role=Role.SUBMITTER, secret="state-secret"
    )

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["sub"] == "dave@example.com"


def test_get_current_user_rejects_env_secret_token_when_app_state_secret_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves precedence, not just "also works": a token signed with the env
    secret must be rejected once app.state.jwt_secret names a different
    value - otherwise both secrets would be silently accepted at once."""
    monkeypatch.setenv("JWT_SECRET", "env-secret")
    app = _build_test_app()
    app.state.jwt_secret = "state-secret"
    client = TestClient(app)
    token = create_access_token(
        subject="dave@example.com", role=Role.SUBMITTER, secret="env-secret"
    )

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_get_current_user_falls_back_to_env_when_app_state_secret_not_set() -> None:
    """No service lifespan has run (or Dapr/the flag was never enabled) -
    app.state has no jwt_secret attribute at all. Must fall back to the env
    var exactly as before this change, not raise AttributeError."""
    client = TestClient(_build_test_app())
    token = create_access_token(subject="erin@example.com", role=Role.SUBMITTER, secret=SECRET)

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
