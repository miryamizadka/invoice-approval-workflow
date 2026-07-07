"""FastAPI transport layer - the only place in this service that knows HTTP.

Endpoints only call IntakeService; all business logic lives there.
"""

from __future__ import annotations

import os

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from services.intake.decision_client import DecisionServiceClient, HttpDecisionServiceClient
from services.intake.logging_config import configure_logging
from services.intake.models import SubmissionStatusResponse
from services.intake.repository import InMemoryInvoiceRepository, InvoiceRepository
from services.intake.service import IntakeService, build_intake_service
from shared.contracts.models import Invoice


def create_app(
    repository: InvoiceRepository | None = None,
    decision_client: DecisionServiceClient | None = None,
) -> FastAPI:
    configure_logging()
    intake_service = build_intake_service(
        repository or InMemoryInvoiceRepository(),
        decision_client
        or HttpDecisionServiceClient(os.environ.get("DECISION_SERVICE_URL", "http://localhost:8001")),
    )

    app = FastAPI(title="ApprovalFlow Intake Service")
    app.state.intake_service = intake_service

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

    return app
