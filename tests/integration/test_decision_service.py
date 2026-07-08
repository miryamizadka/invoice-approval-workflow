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
from shared.contracts.models import (
    DecisionCompletedEvent,
    Recommendation,
    RecommendationType,
    Route,
)


class _StubOutcomePublisher:
    def __init__(self) -> None:
        self.published: list[DecisionCompletedEvent] = []

    async def publish(self, event: DecisionCompletedEvent) -> None:
        self.published.append(event)

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


# --- (g) is_duplicate query param routes to DUPLICATE (needed by Intake) -----


def test_is_duplicate_query_param_routes_to_duplicate() -> None:
    """Decider.decide() already supported is_duplicate; the endpoint didn't
    expose it. Intake needs to pass this through, so it must be reachable
    over HTTP - additive change, default False preserves prior behavior."""
    app = create_app(provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()))
    client = TestClient(app)

    response = client.post("/decisions?is_duplicate=true", json=_invoice_body())

    assert response.status_code == 200
    assert response.json()["route"] == Route.DUPLICATE.value


def test_is_duplicate_defaults_to_false_when_omitted() -> None:
    app = create_app(provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()))
    client = TestClient(app)

    response = client.post("/decisions", json=_invoice_body())

    assert response.status_code == 200
    assert response.json()["route"] == Route.AUTO_APPROVE.value


# --- (h) invoice.submitted subscription handler -------------------------------
#
# Note: DUPLICATE is never reachable through this path - Intake short-circuits
# known duplicates itself before publishing (see IntakeService.process()), so
# the subscription handler always calls decider.decide(..., is_duplicate=False).
# Only AUTO_APPROVE/HUMAN_REVIEW/REJECT are exercised here.


def _post_invoice_submitted(
    client: TestClient, invoice_body: dict[str, Any], correlation_id: str
) -> Any:
    return client.post(
        "/events/invoice-submitted",
        json={"data": {"invoice": invoice_body, "correlation_id": correlation_id}},
    )


def test_invoice_submitted_event_publishes_auto_approve_decision() -> None:
    outcome_publisher = _StubOutcomePublisher()
    app = create_app(
        provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()),
        outcome_publisher=outcome_publisher,
    )
    client = TestClient(app)

    response = _post_invoice_submitted(client, _invoice_body(), "corr-auto-approve")

    assert response.status_code == 200
    assert len(outcome_publisher.published) == 1
    event = outcome_publisher.published[0]
    assert event.decision.route == Route.AUTO_APPROVE
    assert event.decision.correlation_id == "corr-auto-approve"


def test_invoice_submitted_event_publishes_human_review_decision() -> None:
    outcome_publisher = _StubOutcomePublisher()
    app = create_app(
        provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()),
        outcome_publisher=outcome_publisher,
    )
    client = TestClient(app)

    response = _post_invoice_submitted(
        client, _over_ceiling_invoice_body(), "corr-human-review"
    )

    assert response.status_code == 200
    event = outcome_publisher.published[0]
    assert event.decision.route == Route.HUMAN_REVIEW
    assert event.decision.correlation_id == "corr-human-review"


def test_invoice_submitted_event_publishes_reject_decision() -> None:
    severe_reject = Recommendation(
        recommendation=RecommendationType.REJECT,
        confidence=0.95,
        cited_rules=["MEAL-03"],
        reasoning="alcohol-only receipt",
    )
    outcome_publisher = _StubOutcomePublisher()
    app = create_app(
        provider=MockProvider(response=severe_reject.model_dump_json()),
        outcome_publisher=outcome_publisher,
    )
    client = TestClient(app)

    response = _post_invoice_submitted(client, _invoice_body(), "corr-reject")

    assert response.status_code == 200
    event = outcome_publisher.published[0]
    assert event.decision.route == Route.REJECT
    assert event.decision.correlation_id == "corr-reject"


def test_invoice_submitted_event_uses_the_existing_decider_unmodified() -> None:
    """Same MockProvider/AgentError fallback behavior as POST /decisions -
    proves the subscription handler calls the same Decider, not a copy."""
    outcome_publisher = _StubOutcomePublisher()
    app = create_app(provider=_FailingProvider(), outcome_publisher=outcome_publisher)
    client = TestClient(app)

    response = _post_invoice_submitted(client, _invoice_body(), "corr-fallback")

    assert response.status_code == 200
    assert outcome_publisher.published[0].decision.route == Route.HUMAN_REVIEW


# --- (i) DecisionCompletedEvent enrichment (F4 - invoice + recommendation) ----


def test_invoice_submitted_event_publishes_invoice_and_recommendation() -> None:
    """F4: Approval needs the invoice and the agent's recommendation/confidence,
    not just the final route - both must be on the published event."""
    outcome_publisher = _StubOutcomePublisher()
    app = create_app(
        provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()),
        outcome_publisher=outcome_publisher,
    )
    client = TestClient(app)
    invoice_body = _invoice_body()

    _post_invoice_submitted(client, invoice_body, "corr-enriched")

    event = outcome_publisher.published[0]
    assert event.invoice.id == invoice_body["id"]
    assert event.recommendation == VALID_RECOMMENDATION


def test_agent_error_publishes_event_with_no_recommendation() -> None:
    """recommendation is None exactly when the agent itself failed
    (AgentError fallback) - never in the normal AUTO_APPROVE/HUMAN_REVIEW/
    REJECT paths (see the three tests above, all with a real recommendation)."""
    outcome_publisher = _StubOutcomePublisher()
    app = create_app(provider=_FailingProvider(), outcome_publisher=outcome_publisher)
    client = TestClient(app)

    _post_invoice_submitted(client, _invoice_body(), "corr-no-recommendation")

    assert outcome_publisher.published[0].recommendation is None


def test_post_decisions_response_does_not_leak_recommendation() -> None:
    """Regression guard: POST /decisions must keep returning bare Decision
    fields only (D4 API stability) - recommendation must never appear in the
    HTTP response, even though it's now carried internally (DecisionOutcome)
    and on the published event (DecisionCompletedEvent)."""
    app = create_app(provider=MockProvider(response=VALID_RECOMMENDATION.model_dump_json()))
    client = TestClient(app)

    response = client.post("/decisions", json=_invoice_body())

    assert response.status_code == 200
    assert set(response.json().keys()) == {"route", "reason", "triggered_rules", "correlation_id"}
