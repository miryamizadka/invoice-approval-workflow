"""FastAPI transport layer - the only place in this service that knows HTTP
(and, via DaprApp, Dapr's pub/sub subscription wiring).

Endpoints only call IntakeService; all business logic lives there.
"""

from __future__ import annotations

from typing import Any

from dapr.ext.fastapi import DaprApp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from services.intake.dapr_state_repository import DaprStateInvoiceRepository
from services.intake.decision_completed_publisher import (
    DaprDecisionCompletedPublisher,
    DecisionCompletedPublisher,
)
from services.intake.decision_publisher import DaprDecisionPublisher, DecisionPublisher
from services.intake.logging_config import configure_logging
from services.intake.models import SubmissionStatusResponse
from services.intake.repository import InvoiceRepository
from services.intake.service import IntakeService, build_intake_service
from shared.contracts.models import Decision, Invoice


def create_app(
    repository: InvoiceRepository | None = None,
    publisher: DecisionPublisher | None = None,
    decision_completed_publisher: DecisionCompletedPublisher | None = None,
) -> FastAPI:
    configure_logging()
    intake_service = build_intake_service(
        repository or DaprStateInvoiceRepository(),
        publisher or DaprDecisionPublisher(),
        decision_completed_publisher or DaprDecisionCompletedPublisher(),
    )

    app = FastAPI(title="ApprovalFlow Intake Service")
    app.state.intake_service = intake_service
    dapr_app = DaprApp(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "intake-service"}

    @app.post("/invoices", status_code=202)
    async def submit_invoice(
        invoice: Invoice, background_tasks: BackgroundTasks, request: Request, response: Response
    ) -> dict[str, str]:
        service: IntakeService = request.app.state.intake_service
        tracking_id = await service.submit(invoice)
        background_tasks.add_task(service.process, tracking_id)
        response.headers["X-Correlation-Id"] = tracking_id
        return {"tracking_id": tracking_id, "status": "received"}

    @app.get("/invoices/{tracking_id}", response_model=SubmissionStatusResponse)
    async def get_invoice_status(tracking_id: str, request: Request) -> SubmissionStatusResponse:
        service: IntakeService = request.app.state.intake_service
        submission = await service.get_status(tracking_id)
        if submission is None:
            raise HTTPException(status_code=404, detail="tracking_id not found")
        return SubmissionStatusResponse.from_submission(submission)

    @dapr_app.subscribe(
        pubsub="pubsub", topic="decision.completed", route="/events/decision-completed"
    )
    async def handle_decision_completed(request: Request) -> dict[str, str]:
        # dapr-ext-fastapi's subscribe only registers the route (confirmed by
        # reading its source) - it does not unwrap the CloudEvents envelope,
        # so the actual payload is read from the "data" field ourselves.
        body: dict[str, Any] = await request.json()
        decision = Decision.model_validate(body["data"])
        service: IntakeService = request.app.state.intake_service
        await service.complete(decision.correlation_id, decision)
        return {"status": "SUCCESS"}

    return app


app = create_app()
