"""FastAPI transport layer - the only place in this service that knows HTTP
(and, via DaprApp, Dapr's pub/sub subscription wiring) or Postgres startup.
Endpoints only call AuditService; all business logic lives there.

Audit is the first service with genuine async startup work of its own
(schema creation via the lifespan hook below) other than Payment's budget
seeding - same `asynccontextmanager` pattern.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from services.audit.logging_config import configure_logging
from services.audit.models import DashboardSummary
from services.audit.postgres_repository import PostgresAuditRepository
from services.audit.repository import AuditRepository
from services.audit.service import AuditService, build_audit_service
from shared.auth import AuthenticatedUser, Role, require_role
from shared.contracts.models import (
    ApprovalCompletedEvent,
    DecisionCompletedEvent,
    PaymentCompletedEvent,
)
from shared.jwt_secret_loader import load_jwt_secret_from_dapr, resolve_jwt_secret


def create_app(repository: AuditRepository | None = None) -> FastAPI:
    configure_logging()
    resolved_repository = repository or PostgresAuditRepository()
    audit_service = build_audit_service(resolved_repository)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await resolved_repository.ensure_schema()
        # M5/N1 - see services/intake/app.py's identical block for the full
        # reasoning (app.state.jwt_secret precedence, the JWT_SECRET_DAPR_ENABLED
        # gate against DaprClient()'s 60s constructor block in tests).
        if os.environ.get("JWT_SECRET_DAPR_ENABLED", "false").lower() == "true":
            dapr_secret = await load_jwt_secret_from_dapr()
            resolved = resolve_jwt_secret(dapr_secret, os.environ.get("JWT_SECRET"))
            if resolved:
                app.state.jwt_secret = resolved
                if dapr_secret:
                    logging.getLogger(__name__).info("jwt_secret_rebuilt_from_dapr_secret")
        yield

    app = FastAPI(title="ApprovalFlow Audit Service", lifespan=_lifespan)
    app.state.audit_service = audit_service
    dapr_app = DaprApp(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "audit-service"}

    @app.get("/audit/summary", response_model=DashboardSummary)
    async def get_summary(
        request: Request, user: AuthenticatedUser = Depends(require_role(Role.ADMIN))
    ) -> DashboardSummary:
        # Registered BEFORE /audit/{tracking_id} below - FastAPI/Starlette
        # matches routes in registration order, and {tracking_id} is a
        # generic string param that would otherwise swallow "summary" as if
        # it were a tracking_id (same class of hazard as "register /health
        # before the catch-all mount" in services/ui/app.py).
        service: AuditService = request.app.state.audit_service
        return await service.get_summary()

    @app.get("/audit/{tracking_id}")
    async def get_audit_trail(
        tracking_id: str,
        request: Request,
        user: AuthenticatedUser = Depends(require_role(Role.APPROVER)),
    ) -> Any:
        service: AuditService = request.app.state.audit_service
        trail = await service.get_trail(tracking_id)
        if trail is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return trail.model_dump(mode="json")

    @dapr_app.subscribe(
        pubsub="pubsub", topic="decision.completed", route="/events/decision-completed"
    )
    async def handle_decision_completed(request: Request) -> dict[str, str]:
        body: dict[str, Any] = await request.json()
        event = DecisionCompletedEvent.model_validate(body["data"])
        service: AuditService = request.app.state.audit_service
        await service.record_decision_completed(event)
        return {"status": "SUCCESS"}

    @dapr_app.subscribe(
        pubsub="pubsub", topic="approval.completed", route="/events/approval-completed"
    )
    async def handle_approval_completed(request: Request) -> dict[str, str]:
        body: dict[str, Any] = await request.json()
        event = ApprovalCompletedEvent.model_validate(body["data"])
        service: AuditService = request.app.state.audit_service
        await service.record_approval_completed(event)
        return {"status": "SUCCESS"}

    @dapr_app.subscribe(
        pubsub="pubsub", topic="payment.completed", route="/events/payment-completed"
    )
    async def handle_payment_completed(request: Request) -> dict[str, str]:
        body: dict[str, Any] = await request.json()
        event = PaymentCompletedEvent.model_validate(body["data"])
        service: AuditService = request.app.state.audit_service
        await service.record_payment_completed(event)
        return {"status": "SUCCESS"}

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Catches infra failures deliberately never caught inside AuditService
        # (AuditRepositoryError) - logs with correlation_id when available,
        # returns a structured 500. Dapr still sees a non-2xx and retries later.
        correlation_id = request.headers.get("X-Correlation-Id", "unknown")
        logging.getLogger(__name__).exception(
            "unhandled_exception", extra={"correlation_id": correlation_id}
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "correlation_id": correlation_id},
        )

    return app


app = create_app()  # module-level singleton for `uvicorn services.audit.app:app`
