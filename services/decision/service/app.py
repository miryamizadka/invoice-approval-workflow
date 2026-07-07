"""FastAPI transport layer - the only place in this service that knows HTTP.

Endpoints only call Decider.decide(); all business logic lives there.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from services.decision.accessors.factory import get_llm_provider
from services.decision.accessors.llm_provider import LLMProvider
from services.decision.service.decider import Decider, build_decider
from services.decision.service.logging_config import configure_logging
from shared.contracts.models import Decision, Invoice


def create_app(provider: LLMProvider | None = None) -> FastAPI:
    configure_logging()
    decider = build_decider(provider or get_llm_provider())

    app = FastAPI(title="ApprovalFlow Decision Service")
    # Single source of truth - endpoints read it back via request.app.state, not a closure.
    app.state.decider = decider

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "decision-service"}

    @app.post("/decisions", response_model=Decision)
    async def create_decision(
        invoice: Invoice,
        request: Request,
        response: Response,
        x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
    ) -> Decision:
        correlation_id = x_correlation_id or str(uuid.uuid4())
        response.headers["X-Correlation-Id"] = correlation_id
        request_decider: Decider = request.app.state.decider
        return await request_decider.decide(invoice, correlation_id=correlation_id)

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
