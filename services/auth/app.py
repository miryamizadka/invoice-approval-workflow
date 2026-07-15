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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from services.auth.dapr_state_repository import DaprStateUserRepository
from services.auth.demo_users_seeder import seed_demo_users
from services.auth.models import LoginRequest, LoginResponse, RegisterRequest, RegisterResponse
from services.auth.repository import (
    InvalidCredentialsError,
    UserAlreadyExistsError,
    UserRepository,
)
from services.auth.service import AuthService, build_auth_service


def create_app(repository: UserRepository | None = None) -> FastAPI:
    resolved_repository = repository or DaprStateUserRepository()
    auth_service = build_auth_service(resolved_repository)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await seed_demo_users(resolved_repository)
        yield

    app = FastAPI(title="ApprovalFlow Auth Service", lifespan=_lifespan)
    app.state.auth_service = auth_service

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "auth-service"}

    @app.post("/auth/register", response_model=RegisterResponse, status_code=201)
    async def register(payload: RegisterRequest, request: Request) -> RegisterResponse:
        service: AuthService = request.app.state.auth_service
        try:
            return await service.register(payload.email, payload.password)
        except UserAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail="email already registered") from exc

    @app.post("/auth/login", response_model=LoginResponse)
    async def login(payload: LoginRequest, request: Request) -> LoginResponse:
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
