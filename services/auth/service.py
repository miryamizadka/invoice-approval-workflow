"""Transport-agnostic auth business logic - no FastAPI/HTTP knowledge here.
The Manager layer: app.py only calls register()/login()."""

from __future__ import annotations

import os

from services.auth.hashing import hash_password, verify_password
from services.auth.models import LoginResponse, RegisterResponse, User
from services.auth.repository import (
    InvalidCredentialsError,
    UserAlreadyExistsError,
    UserRepository,
)
from shared.auth import DEFAULT_EXPIRY_HOURS, Role, create_access_token


class AuthService:
    def __init__(
        self,
        repository: UserRepository,
        *,
        jwt_secret: str | None,
        expires_hours: int = DEFAULT_EXPIRY_HOURS,
    ) -> None:
        self._repository = repository
        self._jwt_secret = jwt_secret
        self._expires_hours = expires_hours

    async def register(self, email: str, password: str) -> RegisterResponse:
        existing = await self._repository.get_by_email(email)
        if existing is not None:
            raise UserAlreadyExistsError(email)
        password_hash, salt = hash_password(password)
        # Always Role.SUBMITTER - register() has no way to request any
        # other role at all (RegisterRequest has no role field); Approver/
        # Admin accounts only ever come from the seed file (N1 design).
        user = User(email=email, password_hash=password_hash, salt=salt, role=Role.SUBMITTER)
        await self._repository.save(user)
        return RegisterResponse(email=user.email, role=user.role)

    async def login(self, email: str, password: str) -> LoginResponse:
        user = await self._repository.get_by_email(email)
        if user is None or not verify_password(
            password, password_hash=user.password_hash, salt=user.salt
        ):
            # Deliberately the same exception/message for "no such user"
            # and "wrong password" - avoids a user-enumeration signal.
            raise InvalidCredentialsError()
        if not self._jwt_secret:
            # Deferred to first actual login, not construction - a
            # module-level `app = create_app()` singleton must never
            # hard-fail at import time on missing config (same resilience
            # posture as every other service in this project, e.g.
            # Decision's LLM provider resolution).
            raise RuntimeError("JWT_SECRET not configured")
        token = create_access_token(
            subject=user.email,
            role=user.role,
            secret=self._jwt_secret,
            expires_hours=self._expires_hours,
        )
        return LoginResponse(access_token=token)


def build_auth_service(
    repository: UserRepository, *, jwt_secret: str | None = None, expires_hours: int | None = None
) -> AuthService:
    hours = expires_hours or int(os.environ.get("JWT_EXPIRY_HOURS", DEFAULT_EXPIRY_HOURS))
    return AuthService(
        repository,
        jwt_secret=jwt_secret or os.environ.get("JWT_SECRET"),
        expires_hours=hours,
    )
