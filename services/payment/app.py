"""FastAPI transport layer - the only place in this service that knows HTTP
(and, via DaprApp, Dapr's pub/sub subscription wiring).

Endpoints only call PaymentService; all business logic lives there.

Payment is the first service with genuine async startup work (budget
seeding via the lifespan hook below) - see test_payment_service.py's module
docstring for the resulting TestClient usage requirement.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dapr.ext.fastapi import DaprApp
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from services.payment.accessors.payment_gateway import PaymentGateway
from services.payment.accessors.simulated_gateway import SimulatedPaymentGateway
from services.payment.budgets_loader import load_budgets
from services.payment.dapr_state_repository import (
    DaprStateBudgetRepository,
    DaprStatePaymentRepository,
)
from services.payment.logging_config import configure_logging
from services.payment.models import Budget, PaymentRecord
from services.payment.outcome_publisher import DaprPaymentOutcomePublisher, PaymentOutcomePublisher
from services.payment.repository import BudgetNotFoundError, BudgetRepository, PaymentRepository
from services.payment.service import (
    PaymentNotFoundError,
    PaymentService,
    build_payment_service,
)
from shared.auth import AuthenticatedUser, Role, get_current_user, require_role
from shared.contracts.models import ApprovalCompletedEvent, DecisionCompletedEvent


def create_app(
    repository: PaymentRepository | None = None,
    budget_repository: BudgetRepository | None = None,
    publisher: PaymentOutcomePublisher | None = None,
    gateway: PaymentGateway | None = None,
) -> FastAPI:
    configure_logging()
    resolved_budget_repository = budget_repository or DaprStateBudgetRepository()
    payment_service = build_payment_service(
        repository or DaprStatePaymentRepository(),
        resolved_budget_repository,
        publisher or DaprPaymentOutcomePublisher(),
        gateway or SimulatedPaymentGateway(),
    )

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        for department, total in load_budgets().items():
            await resolved_budget_repository.ensure_seeded(department, total)
        yield

    app = FastAPI(title="ApprovalFlow Payment Service", lifespan=_lifespan)
    app.state.payment_service = payment_service
    app.state.budget_repository = resolved_budget_repository
    dapr_app = DaprApp(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "payment-service"}

    @app.get("/payments", response_model=list[PaymentRecord])
    async def list_payments(
        request: Request, user: AuthenticatedUser = Depends(require_role(Role.ADMIN))
    ) -> list[PaymentRecord]:
        service: PaymentService = request.app.state.payment_service
        return await service.list_all()

    @app.get("/payments/{tracking_id}", response_model=PaymentRecord)
    async def get_payment(
        tracking_id: str,
        request: Request,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> PaymentRecord:
        service: PaymentService = request.app.state.payment_service
        try:
            return await service.get(tracking_id)
        except PaymentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="tracking_id not found") from exc

    @app.get("/budgets/{department}", response_model=Budget)
    async def get_budget(
        department: str,
        request: Request,
        user: AuthenticatedUser = Depends(require_role(Role.ADMIN)),
    ) -> Budget:
        service: PaymentService = request.app.state.payment_service
        try:
            return await service.get_budget(department)
        except BudgetNotFoundError as exc:
            raise HTTPException(status_code=404, detail="department not found") from exc

    @dapr_app.subscribe(
        pubsub="pubsub", topic="decision.completed", route="/events/decision-completed"
    )
    async def handle_decision_completed(request: Request) -> dict[str, str]:
        # dapr-ext-fastapi's subscribe only registers the route (confirmed by
        # reading its source, see prior phases) - it does not unwrap the
        # CloudEvents envelope, so the payload is read from "data" ourselves.
        body: dict[str, Any] = await request.json()
        event = DecisionCompletedEvent.model_validate(body["data"])
        service: PaymentService = request.app.state.payment_service
        await service.handle_decision_completed(event)
        return {"status": "SUCCESS"}

    @dapr_app.subscribe(
        pubsub="pubsub", topic="approval.completed", route="/events/approval-completed"
    )
    async def handle_approval_completed(request: Request) -> dict[str, str]:
        body: dict[str, Any] = await request.json()
        event = ApprovalCompletedEvent.model_validate(body["data"])
        service: PaymentService = request.app.state.payment_service
        await service.handle_approval_completed(event)
        return {"status": "SUCCESS"}

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Catches infra failures deliberately never caught inside PaymentService
        # (BudgetRepositoryError, PaymentRepositoryError, PaymentOutcomePublisherError,
        # etc.) - logs with correlation_id when available, returns a structured 500
        # instead of a bare traceback. Dapr still sees a non-2xx and retries later.
        correlation_id = request.headers.get("X-Correlation-Id", "unknown")
        logging.getLogger(__name__).exception(
            "unhandled_exception", extra={"correlation_id": correlation_id}
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "correlation_id": correlation_id},
        )

    return app


app = create_app()  # module-level singleton for `uvicorn services.payment.app:app`
