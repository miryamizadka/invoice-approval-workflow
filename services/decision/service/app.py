"""FastAPI transport layer - the only place in this service that knows HTTP
(and, via DaprApp, Dapr's pub/sub subscription wiring).

Endpoints only call Decider.decide(); all business logic lives there.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from dapr.ext.fastapi import DaprApp
from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from services.decision.accessors.factory import get_llm_provider
from services.decision.accessors.llm_provider import LLMProvider
from services.decision.service.decider import Decider, build_decider
from services.decision.service.logging_config import configure_logging
from services.decision.service.outcome_publisher import (
    DaprDecisionOutcomePublisher,
    DecisionOutcomePublisher,
)
from shared.contracts.models import Decision, Invoice, InvoiceSubmittedEvent


def create_app(
    provider: LLMProvider | None = None,
    outcome_publisher: DecisionOutcomePublisher | None = None,
) -> FastAPI:
    configure_logging()
    decider = build_decider(provider or get_llm_provider())
    resolved_outcome_publisher = outcome_publisher or DaprDecisionOutcomePublisher()

    app = FastAPI(title="ApprovalFlow Decision Service")
    # Single source of truth - endpoints read it back via request.app.state, not a closure.
    app.state.decider = decider
    app.state.outcome_publisher = resolved_outcome_publisher
    dapr_app = DaprApp(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "decision-service"}

    @app.post("/decisions", response_model=Decision)
    async def create_decision(
        invoice: Invoice,
        request: Request,
        response: Response,
        is_duplicate: bool = Query(default=False),
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Decision:
        correlation_id = x_correlation_id or str(uuid.uuid4())
        response.headers["X-Correlation-Id"] = correlation_id
        request_decider: Decider = request.app.state.decider
        return await request_decider.decide(
            invoice, correlation_id=correlation_id, is_duplicate=is_duplicate
        )

    @dapr_app.subscribe(
        pubsub="pubsub", topic="invoice.submitted", route="/events/invoice-submitted"
    )
    async def handle_invoice_submitted(request: Request) -> dict[str, str]:
        # dapr-ext-fastapi's subscribe only registers the route (confirmed by
        # reading its source) - it does not unwrap the CloudEvents envelope,
        # so the actual payload is read from the "data" field ourselves.
        body: dict[str, Any] = await request.json()
        event = InvoiceSubmittedEvent.model_validate(body["data"])
        request_decider: Decider = request.app.state.decider
        request_outcome_publisher: DecisionOutcomePublisher = request.app.state.outcome_publisher
        # is_duplicate is always False here: Intake never publishes for an
        # invoice it already knows is a duplicate (see IntakeService.process()'s
        # short-circuit) - reaching this handler already proves that.
        decision = await request_decider.decide(
            event.invoice, correlation_id=event.correlation_id, is_duplicate=False
        )
        await request_outcome_publisher.publish(decision)
        return {"status": "SUCCESS"}

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


app = create_app()  # module-level singleton for `uvicorn services.decision.service.app:app`
