"""FastAPI transport layer - the only place in this service that knows HTTP
(and, via DaprApp, Dapr's pub/sub subscription wiring).

Endpoints only call Decider.decide(); all business logic lives there.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dapr.ext.fastapi import DaprApp
from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from services.decision.accessors.bulkhead_llm_provider import BulkheadLLMProvider
from services.decision.accessors.factory import get_llm_provider
from services.decision.accessors.llm_provider import LLMProvider
from services.decision.router.config import DEFAULT_THRESHOLDS
from services.decision.service.dapr_config_loader import load_policy_and_thresholds
from services.decision.service.dapr_secret_loader import (
    load_groq_api_key,
    resolve_provider_from_secret,
)
from services.decision.service.decider import Decider, build_decider
from services.decision.service.logging_config import configure_logging
from services.decision.service.outcome_publisher import (
    DaprDecisionOutcomePublisher,
    DecisionOutcomePublisher,
)
from services.decision.service.policy_loader import load_policy_text
from shared.contracts.models import Decision, DecisionCompletedEvent, Invoice, InvoiceSubmittedEvent


def create_app(
    provider: LLMProvider | None = None,
    outcome_publisher: DecisionOutcomePublisher | None = None,
) -> FastAPI:
    configure_logging()
    # N3: wrapped exactly once, here - never inside build_decider()/Decider,
    # which stay provider-implementation-agnostic. The `is not` identity
    # check below (in _lifespan) is what prevents this from ever being
    # re-wrapped when the provider is unchanged.
    resolved_provider = BulkheadLLMProvider(provider or get_llm_provider())
    fallback_policy = load_policy_text()  # read once, reused below - not twice
    decider = build_decider(resolved_provider, DEFAULT_THRESHOLDS, fallback_policy)
    resolved_outcome_publisher = outcome_publisher or DaprDecisionOutcomePublisher()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # M5: overrides the env-var-based GroqProvider with one built from
        # Dapr's secret store, if LLM_PROVIDER=groq and a value is available -
        # same fetch-once, resilient-fallback posture as the threshold/policy
        # override below. `if provider is None` - never second-guesses a
        # provider a caller/test injected explicitly.
        active_provider = resolved_provider
        if provider is None and os.environ.get("LLM_PROVIDER", "mock").lower() == "groq":
            secret_key = await load_groq_api_key()
            rebuilt = resolve_provider_from_secret(secret_key, resolved_provider)
            if rebuilt is not resolved_provider:
                # A genuinely new raw provider was built from the secret -
                # wrap *that* one (N3), not resolved_provider again.
                active_provider = BulkheadLLMProvider(rebuilt)
                logging.getLogger(__name__).info("llm_provider_rebuilt_from_dapr_secret")
        # F7/M13: overrides DEFAULT_THRESHOLDS/policy.md with whatever is
        # configured in Dapr's configuration store, if anything - fetch-once,
        # not live hot-reload (see ADR-009). Falls back silently to the
        # already-built `decider` above if the store is empty/unreachable -
        # the service must work correctly even if never configured.
        policy, thresholds = await load_policy_and_thresholds(fallback_policy, DEFAULT_THRESHOLDS)
        if (
            policy != fallback_policy
            or thresholds != DEFAULT_THRESHOLDS
            or active_provider is not resolved_provider
        ):
            app.state.decider = build_decider(active_provider, thresholds, policy)
        yield

    app = FastAPI(title="ApprovalFlow Decision Service", lifespan=_lifespan)
    # Single source of truth - endpoints read it back via request.app.state, not a closure.
    app.state.decider = decider  # fallback; _lifespan may replace it once Dapr config is read
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
        outcome = await request_decider.decide(
            invoice, correlation_id=correlation_id, is_duplicate=is_duplicate
        )
        return outcome.decision

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
        outcome = await request_decider.decide(
            event.invoice, correlation_id=event.correlation_id, is_duplicate=False
        )
        completed_event = DecisionCompletedEvent(
            invoice=event.invoice,
            decision=outcome.decision,
            recommendation=outcome.recommendation,
        )
        await request_outcome_publisher.publish(completed_event)
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
