"""Integration tests for the Decision Service HTTP API.

Exercises the full chain: HTTP -> FastAPI -> Decider -> Agent graph -> Router
-> Decision, with MockProvider/a stub - no real network call (ADR-005).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from services.decision.accessors.llm_provider import LLMProviderError
from services.decision.accessors.mock_provider import MockProvider
from services.decision.service.app import create_app
from shared.contracts.models import Recommendation, RecommendationType, Route

VALID_RECOMMENDATION = Recommendation(
    recommendation=RecommendationType.APPROVE,
    confidence=0.9,
    cited_rules=[],
    reasoning="clean",
)


class _FailingProvider:
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[Any] | None = None,
    ) -> str:
        raise LLMProviderError("provider is unavailable")


def _invoice_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "TEST-0001",
        "submitter": "test@example.com",
        "department": "engineering-2026Q2",
        "vendor": "Test Vendor",
        "vendorKnown": True,
        "invoiceNumber": "INV-0001",
        "currency": "USD",
        "category": "meals",
        "attendees": 1,
        "lineItems": [{"description": "Test item", "quantity": "1", "unitPrice": "50.00"}],
        "taxAmount": "0.00",
        "total": "50.00",
        "receiptPresent": True,
        "date": "2026-05-12",
        "notes": None,
    }
    body.update(overrides)
    return body


def _over_ceiling_invoice_body() -> dict[str, Any]:
    return _invoice_body(
        category="other",
        attendees=None,
        lineItems=[{"description": "Test item", "quantity": "1", "unitPrice": "300.00"}],
        total="300.00",
    )


# --- (a) valid invoice -> decision -------------------------------------------


def test_valid_invoice_returns_decision() -> None:
    app = create_app(provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()))
    client = TestClient(app)

    response = client.post("/decisions", json=_invoice_body())

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == Route.AUTO_APPROVE.value


# --- (b) invalid invoice -> 422 ------------------------------------------------


def test_invalid_invoice_returns_422() -> None:
    app = create_app(provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()))
    client = TestClient(app)

    incomplete_body = {"id": "TEST-0002"}  # missing every other required field
    response = client.post("/decisions", json=incomplete_body)

    assert response.status_code == 422


# --- (c) AgentError -> fallback, not a crash -----------------------------------


def test_agent_error_falls_back_to_human_review_not_a_crash() -> None:
    app = create_app(provider=_FailingProvider())
    client = TestClient(app)

    response = client.post("/decisions", json=_invoice_body())

    assert response.status_code == 200
    assert response.json()["route"] == Route.HUMAN_REVIEW.value


# --- (d) health check -----------------------------------------------------------


def test_health_check() -> None:
    app = create_app(provider=MockProvider())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "decision-service"}


# --- (e) correlation_id ----------------------------------------------------------


def test_correlation_id_is_echoed_when_supplied() -> None:
    app = create_app(provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()))
    client = TestClient(app)

    response = client.post(
        "/decisions", json=_invoice_body(), headers={"X-Correlation-Id": "test-correlation-123"}
    )

    assert response.status_code == 200
    assert response.headers["X-Correlation-Id"] == "test-correlation-123"
    assert response.json()["correlation_id"] == "test-correlation-123"


def test_correlation_id_is_generated_when_absent() -> None:
    app = create_app(provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()))
    client = TestClient(app)

    response = client.post("/decisions", json=_invoice_body())

    assert response.status_code == 200
    generated = response.headers["X-Correlation-Id"]
    assert generated  # non-empty
    assert response.json()["correlation_id"] == generated


# --- (f) M12 end-to-end through HTTP: the router is the sole authority --------


def test_m12_ceiling_survives_optimistic_agent_through_http() -> None:
    """An invoice over the autonomy ceiling, with the (mocked) LLM confidently
    recommending approval, must still come back human_review - proven through
    the real HTTP transport, not just at the router/agent unit level."""
    optimistic_approval = Recommendation(
        recommendation=RecommendationType.APPROVE,
        confidence=1.0,
        cited_rules=[],
        reasoning="looks fine to me",
    )
    app = create_app(provider=MockProvider(response=optimistic_approval.model_dump_json()))
    client = TestClient(app)

    response = client.post("/decisions", json=_over_ceiling_invoice_body())

    assert response.status_code == 200
    assert response.json()["route"] == Route.HUMAN_REVIEW.value
