"""FastAPI transport layer - the only place in this service that knows HTTP
(and, via DaprApp, Dapr's pub/sub subscription wiring).

Endpoints only call ApprovalService; all business logic lives there.
"""

from __future__ import annotations

from typing import Any

from dapr.ext.fastapi import DaprApp
from fastapi import FastAPI, HTTPException, Request

from services.approval.dapr_state_repository import DaprStateApprovalRepository
from services.approval.logging_config import configure_logging
from services.approval.models import PendingApproval
from services.approval.outcome_publisher import (
    ApprovalOutcomePublisher,
    DaprApprovalOutcomePublisher,
)
from services.approval.repository import ApprovalRepository
from services.approval.service import (
    ApprovalAlreadyResolvedError,
    ApprovalNotFoundError,
    ApprovalService,
    build_approval_service,
)
from shared.contracts.models import DecisionCompletedEvent


def create_app(
    repository: ApprovalRepository | None = None,
    publisher: ApprovalOutcomePublisher | None = None,
) -> FastAPI:
    configure_logging()
    approval_service = build_approval_service(
        repository or DaprStateApprovalRepository(),
        publisher or DaprApprovalOutcomePublisher(),
    )

    app = FastAPI(title="ApprovalFlow Approval Service")
    app.state.approval_service = approval_service
    dapr_app = DaprApp(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "approval-service"}

    @app.get("/approvals", response_model=list[PendingApproval])
    async def list_approvals(request: Request) -> list[PendingApproval]:
        service: ApprovalService = request.app.state.approval_service
        return await service.list_pending()

    @app.get("/approvals/{tracking_id}", response_model=PendingApproval)
    async def get_approval(tracking_id: str, request: Request) -> PendingApproval:
        service: ApprovalService = request.app.state.approval_service
        try:
            return await service.get(tracking_id)
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail="tracking_id not found") from exc

    @app.post("/approvals/{tracking_id}/approve", response_model=PendingApproval)
    async def approve(tracking_id: str, request: Request) -> PendingApproval:
        service: ApprovalService = request.app.state.approval_service
        return await _resolve(service.approve, tracking_id)

    @app.post("/approvals/{tracking_id}/reject", response_model=PendingApproval)
    async def reject(tracking_id: str, request: Request) -> PendingApproval:
        service: ApprovalService = request.app.state.approval_service
        return await _resolve(service.reject, tracking_id)

    @app.post("/approvals/{tracking_id}/request-info", response_model=PendingApproval)
    async def request_info(tracking_id: str, request: Request) -> PendingApproval:
        service: ApprovalService = request.app.state.approval_service
        return await _resolve(service.request_info, tracking_id)

    @dapr_app.subscribe(
        pubsub="pubsub", topic="decision.completed", route="/events/decision-completed"
    )
    async def handle_decision_completed(request: Request) -> dict[str, str]:
        # dapr-ext-fastapi's subscribe only registers the route (confirmed by
        # reading its source) - it does not unwrap the CloudEvents envelope,
        # so the actual payload is read from the "data" field ourselves.
        body: dict[str, Any] = await request.json()
        event = DecisionCompletedEvent.model_validate(body["data"])
        service: ApprovalService = request.app.state.approval_service
        await service.handle_decision_completed(event)
        return {"status": "SUCCESS"}

    return app


async def _resolve(action: Any, tracking_id: str) -> PendingApproval:
    try:
        result: PendingApproval = await action(tracking_id)
        return result
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="tracking_id not found") from exc
    except ApprovalAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail="approval already resolved") from exc


app = create_app()  # module-level singleton for `uvicorn services.approval.app:app`
