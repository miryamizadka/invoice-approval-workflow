"""FastAPI transport layer - the only place in this service that knows
HTTP. POST /auth/register (Submitter self-service only - see
AuthService.register()) and POST /auth/login issue/verify JWTs via
shared/auth.py.

Startup lifespan seeds the demo Approver/Admin accounts
(services/auth/demo_users.json) - same asynccontextmanager pattern as
Payment's own budget seeding.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from services.auth.dapr_state_repository import DaprStateUserRepository
from services.auth.demo_users_seeder import seed_demo_users
from services.auth.models import LoginRequest, LoginResponse, RegisterRequest, RegisterResponse
from services.auth.rate_limit import throttle_login, throttle_register
from services.auth.repository import (
    InvalidCredentialsError,
    UserAlreadyExistsError,
    UserRepository,
)
from services.auth.service import AuthService, build_auth_service
from shared.rate_limiter import DaprStateRateLimiter, RateLimiter

_DEFAULT_AUTH_REGISTER_RATE_LIMIT = 5  # comfortably above verify_auth.py's
# own measured traffic (2 register calls per run: one real, one duplicate).
_DEFAULT_AUTH_REGISTER_RATE_LIMIT_WINDOW_SECONDS = 60
_DEFAULT_AUTH_LOGIN_RATE_LIMIT = 20  # deliberately generous: the seeded
# demo Approver/Admin accounts get logged into repeatedly by every verify
# script AND by every manual re-run during development - a tight limit
# here risks a self-inflicted lockout on the shared demo accounts, which
# would look exactly like a regression but wouldn't be one.
_DEFAULT_AUTH_LOGIN_RATE_LIMIT_WINDOW_SECONDS = 60


def create_app(
    repository: UserRepository | None = None,
    rate_limiter: RateLimiter | None = None,
    auth_register_rate_limit: int | None = None,
    auth_register_rate_limit_window_seconds: int | None = None,
    auth_login_rate_limit: int | None = None,
    auth_login_rate_limit_window_seconds: int | None = None,
) -> FastAPI:
    resolved_repository = repository or DaprStateUserRepository()
    auth_service = build_auth_service(resolved_repository)

    resolved_register_limit = auth_register_rate_limit
    if resolved_register_limit is None:
        resolved_register_limit = int(
            os.environ.get(
                "AUTH_REGISTER_RATE_LIMIT", str(_DEFAULT_AUTH_REGISTER_RATE_LIMIT)
            )
        )
    resolved_register_window_seconds = auth_register_rate_limit_window_seconds
    if resolved_register_window_seconds is None:
        resolved_register_window_seconds = int(
            os.environ.get(
                "AUTH_REGISTER_RATE_LIMIT_WINDOW_SECONDS",
                str(_DEFAULT_AUTH_REGISTER_RATE_LIMIT_WINDOW_SECONDS),
            )
        )
    resolved_login_limit = auth_login_rate_limit
    if resolved_login_limit is None:
        resolved_login_limit = int(
            os.environ.get("AUTH_LOGIN_RATE_LIMIT", str(_DEFAULT_AUTH_LOGIN_RATE_LIMIT))
        )
    resolved_login_window_seconds = auth_login_rate_limit_window_seconds
    if resolved_login_window_seconds is None:
        resolved_login_window_seconds = int(
            os.environ.get(
                "AUTH_LOGIN_RATE_LIMIT_WINDOW_SECONDS",
                str(_DEFAULT_AUTH_LOGIN_RATE_LIMIT_WINDOW_SECONDS),
            )
        )

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await seed_demo_users(resolved_repository)
        yield

    app = FastAPI(title="ApprovalFlow Auth Service", lifespan=_lifespan)
    app.state.auth_service = auth_service
    app.state.rate_limiter = rate_limiter or DaprStateRateLimiter()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "auth-service"}

    @app.post("/auth/register", response_model=RegisterResponse, status_code=201)
    async def register(
        payload: RegisterRequest,
        request: Request,
        _: None = Depends(
            throttle_register(
                limit=resolved_register_limit, window_seconds=resolved_register_window_seconds
            )
        ),
    ) -> RegisterResponse:
        service: AuthService = request.app.state.auth_service
        try:
            return await service.register(payload.email, payload.password)
        except UserAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail="email already registered") from exc

    @app.post("/auth/login", response_model=LoginResponse)
    async def login(
        payload: LoginRequest,
        request: Request,
        _: None = Depends(
            throttle_login(
                limit=resolved_login_limit, window_seconds=resolved_login_window_seconds
            )
        ),
    ) -> LoginResponse:
        service: AuthService = request.app.state.auth_service
        try:
            return await service.login(payload.email, payload.password)
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=401, detail="invalid email or password") from exc

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = request.headers.get("X-Correlation-Id", "unknown")
        logging.getLogger(__name__).exception(
            "unhandled_exception", extra={"correlation_id": correlation_id}
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "correlation_id": correlation_id},
        )

    return app


app = create_app()  # module-level singleton for `uvicorn services.auth.app:app`
