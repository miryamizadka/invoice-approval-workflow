"""Integration tests for the Intake Service HTTP API.

Exercises HTTP -> IntakeService -> InMemoryInvoiceRepository, with a stub
DecisionPublisher - no real Dapr sidecar. The decision.completed subscription
route is exercised directly via TestClient, POSTing a CloudEvent-shaped body
(just the `data` field - that's all the handler reads) to simulate what the
Dapr sidecar would deliver.

N1: _build_app() authenticates every test as Role.SUBMITTER (the floor role
for both of Intake's routes) via app.dependency_overrides[get_current_user] -
the standard FastAPI testing idiom. The dedicated 401 tests at the bottom
clear that override deliberately to prove the gate itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.intake.app import create_app
from services.intake.approval_status_client import ApprovalStatusClient, ApprovalStatusClientError
from services.intake.decision_completed_publisher import DecisionCompletedPublisher
from services.intake.decision_publisher import DecisionPublisher, DecisionPublisherError
from services.intake.models import ApprovalStatusSnapshot
from services.intake.repository import InMemoryInvoiceRepository, InvoiceRepository
from shared.auth import AuthenticatedUser, Role, get_current_user
from shared.contracts.models import (
    DecisionCompletedEvent,
    Invoice,
    Recommendation,
    RecommendationType,
    Route,
)
from tests.support.event_fixtures import decision_completed_event

_TEST_SUBMITTER_EMAIL = "test-submitter@example.com"


class _StubPublisher:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def publish(self, invoice: Invoice, *, correlation_id: str) -> None:
        self.calls.append({"invoice": invoice, "correlation_id": correlation_id})
        if self._error is not None:
            raise self._error


class _StubDecisionCompletedPublisher:
    def __init__(self) -> None:
        self.calls: list[DecisionCompletedEvent] = []

    async def publish(self, event: DecisionCompletedEvent) -> None:
        self.calls.append(event)


class _StubApprovalStatusClient:
    def __init__(
        self, *, snapshot: ApprovalStatusSnapshot | None = None, error: Exception | None = None
    ) -> None:
        self._snapshot = snapshot
        self._error = error

    async def get_status(self, tracking_id: str) -> ApprovalStatusSnapshot | None:
        if self._error is not None:
            raise self._error
        return self._snapshot


class _NeverCalledApprovalStatusClient:
    """Default for tests that never escalate to human_review - if this gets
    called, something's wrong (see get_status_response's route gate)."""

    async def get_status(self, tracking_id: str) -> ApprovalStatusSnapshot | None:
        raise AssertionError("not expected to be called for a non-human_review submission")


def _build_app(
    publisher: DecisionPublisher,
    repository: InvoiceRepository | None = None,
    decision_completed_publisher: DecisionCompletedPublisher | None = None,
    approval_status_client: ApprovalStatusClient | None = None,
) -> FastAPI:
    app = create_app(
        repository=repository or InMemoryInvoiceRepository(),
        publisher=publisher,
        decision_completed_publisher=decision_completed_publisher
        or _StubDecisionCompletedPublisher(),
        approval_status_client=approval_status_client or _NeverCalledApprovalStatusClient(),
    )
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub=_TEST_SUBMITTER_EMAIL, role=Role.SUBMITTER
    )
    return app


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


def _post_decision_completed(
    client: TestClient,
    route: Route = Route.AUTO_APPROVE,
    *,
    correlation_id: str = "cid",
    invoice: Invoice | None = None,
    recommendation: Recommendation | None = None,
) -> Any:
    """Builds the real decision.completed payload shape via the shared
    tests/support/event_fixtures builder - a DecisionCompletedEvent (invoice +
    decision + recommendation), matching what Decision Service actually
    publishes in production, not a bare Decision. Deliberately built via the
    shared Pydantic model, not a hand-rolled dict, so any future drift between
    this and the real contract fails the test instead of hiding behind a
    synthetic payload."""
    event = decision_completed_event(
        route, correlation_id=correlation_id, invoice=invoice, recommendation=recommendation
    )
    return client.post(
        "/events/decision-completed", json={"data": event.model_dump(mode="json")}
    )


# --- (a) submit returns tracking id immediately -------------------------------


def test_submit_returns_tracking_id_immediately() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    response = client.post("/invoices", json=_invoice_body())

    assert response.status_code == 202
    assert response.json()["tracking_id"]
    assert response.headers["X-Correlation-Id"] == response.json()["tracking_id"]


# --- (b) status is processing after publish, not completed --------------------


def test_status_is_processing_after_background_publish() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]
    status = client.get(f"/invoices/{tracking_id}")

    assert status.status_code == 200
    assert status.json()["status"] == "processing"


# --- (c) decision.completed event completes the submission --------------------


def test_decision_completed_event_completes_the_submission() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)
    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]

    event_response = _post_decision_completed(
        client, Route.AUTO_APPROVE, correlation_id=tracking_id
    )
    status = client.get(f"/invoices/{tracking_id}")

    assert event_response.status_code == 200
    body = status.json()
    assert body["status"] == "completed"
    assert body["decision"]["route"] == Route.AUTO_APPROVE.value


def test_decision_completed_event_with_recommendation_is_parsed_correctly() -> None:
    """Regression test for a real bug found live in docker compose: Decision
    Service publishes the enriched DecisionCompletedEvent (invoice + decision +
    recommendation), not a bare Decision - the handler previously crashed with a
    pydantic ValidationError on this exact shape (a `recommendation` field is
    what triggered extra_forbidden). event.recommendation is parsed here but
    intentionally unused by Intake's business logic - it's forwarded only
    because it's part of the shared contract with Approval/Notification."""
    app = _build_app(_StubPublisher())
    client = TestClient(app)
    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]
    recommendation = Recommendation(
        recommendation=RecommendationType.APPROVE,
        confidence=0.9,
        cited_rules=[],
        reasoning="stub: optimistic non-adversarial recommendation",
    )

    event_response = _post_decision_completed(
        client, Route.AUTO_APPROVE, correlation_id=tracking_id, recommendation=recommendation
    )
    status = client.get(f"/invoices/{tracking_id}")

    assert event_response.status_code == 200
    assert status.json()["status"] == "completed"


# --- (d) duplicate short-circuits: no invoice.submitted, immediately completed,
#         publishes decision.completed directly instead --------------------------


def test_duplicate_submission_completes_immediately_and_publishes_decision_completed() -> None:
    publisher = _StubPublisher()
    decision_completed_publisher = _StubDecisionCompletedPublisher()
    app = _build_app(publisher, decision_completed_publisher=decision_completed_publisher)
    client = TestClient(app)
    body = _invoice_body()

    client.post("/invoices", json=body)
    second_tracking_id = client.post("/invoices", json=body).json()["tracking_id"]

    assert len(publisher.calls) == 1  # only the first (non-duplicate) publish
    status = client.get(f"/invoices/{second_tracking_id}")
    assert status.json()["status"] == "completed"
    assert status.json()["decision"]["route"] == Route.DUPLICATE.value
    assert len(decision_completed_publisher.calls) == 1
    assert decision_completed_publisher.calls[0].decision.route == Route.DUPLICATE
    assert decision_completed_publisher.calls[0].decision.correlation_id == second_tracking_id


# --- (e) invalid invoice -> 422 -----------------------------------------------


def test_invalid_invoice_returns_422() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    response = client.post("/invoices", json={"id": "TEST-0002"})

    assert response.status_code == 422


# --- (f) dedup_key not exposed in API response --------------------------------


def test_dedup_key_not_exposed_in_status_response() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]
    status = client.get(f"/invoices/{tracking_id}")

    assert "dedup_key" not in status.json()


# --- (g) unknown tracking_id -> 404 -------------------------------------------


def test_unknown_tracking_id_returns_404() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    response = client.get("/invoices/does-not-exist")

    assert response.status_code == 404


# --- (h) publish failure -> failed, not a crash, no raw leak ------------------


def test_publish_failure_marks_status_failed_without_leaking_raw_error() -> None:
    error = DecisionPublisherError("Failed to publish invoice.submitted: connect to internal-host")
    app = _build_app(_StubPublisher(error=error))
    client = TestClient(app)

    submit = client.post("/invoices", json=_invoice_body())
    assert submit.status_code == 202
    tracking_id = submit.json()["tracking_id"]

    status = client.get(f"/invoices/{tracking_id}")

    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "failed"
    assert "internal-host" not in (body.get("reason") or "")


# --- (i) decision.completed for an unknown tracking_id doesn't crash ----------


def test_decision_completed_for_unknown_tracking_id_does_not_crash() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    response = _post_decision_completed(client, correlation_id="does-not-exist")

    assert response.status_code == 200


# --- (j) M5 service invocation: GET /invoices/{id} enriched with live
#         Approval Service status for a human_review submission --------------


def test_status_enriched_with_live_approval_status_for_human_review() -> None:
    approval_client = _StubApprovalStatusClient(
        snapshot=ApprovalStatusSnapshot(status="pending", additional_info=None)
    )
    app = _build_app(_StubPublisher(), approval_status_client=approval_client)
    client = TestClient(app)
    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]

    _post_decision_completed(client, Route.HUMAN_REVIEW, correlation_id=tracking_id)
    status = client.get(f"/invoices/{tracking_id}")

    assert status.status_code == 200
    assert status.json()["approval"] == {"status": "pending", "additional_info": None}


def test_status_approval_null_for_non_human_review() -> None:
    app = _build_app(_StubPublisher())  # default client raises if ever called
    client = TestClient(app)
    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]

    _post_decision_completed(client, Route.AUTO_APPROVE, correlation_id=tracking_id)
    status = client.get(f"/invoices/{tracking_id}")

    assert status.status_code == 200
    assert status.json()["approval"] is None


def test_status_degrades_gracefully_when_approval_service_returns_500() -> None:
    """Approval Service being unreachable/erroring must not turn Intake's own
    GET /invoices/{id} into a 500 - the frozen decision is still valid data."""
    approval_client = _StubApprovalStatusClient(
        error=ApprovalStatusClientError("Approval Service invocation failed: 500")
    )
    app = _build_app(_StubPublisher(), approval_status_client=approval_client)
    client = TestClient(app)
    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]

    _post_decision_completed(client, Route.HUMAN_REVIEW, correlation_id=tracking_id)
    status = client.get(f"/invoices/{tracking_id}")

    assert status.status_code == 200
    body = status.json()
    assert body["approval"] is None
    assert body["decision"]["route"] == Route.HUMAN_REVIEW.value


# --- (j) health check ----------------------------------------------------------


def test_health_check() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "intake-service"}


# --- N1: authentication + submitter identity derivation -----------------------


def test_submit_requires_authentication() -> None:
    app = _build_app(_StubPublisher())
    app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(app)

    response = client.post("/invoices", json=_invoice_body())

    assert response.status_code == 401


def test_get_invoice_status_requires_authentication() -> None:
    app = _build_app(_StubPublisher())
    client = TestClient(app)
    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]
    app.dependency_overrides.pop(get_current_user, None)

    response = client.get(f"/invoices/{tracking_id}")

    assert response.status_code == 401


def test_submit_forces_submitter_to_the_authenticated_identity() -> None:
    """Anti-spoofing (N1): the client-supplied `submitter` field must be
    ignored and overwritten with the JWT identity - otherwise anyone could
    submit "as" a different, fake submitter."""
    app = _build_app(_StubPublisher())
    client = TestClient(app)

    tracking_id = client.post(
        "/invoices", json=_invoice_body(submitter="attacker@example.com")
    ).json()["tracking_id"]

    status = client.get(f"/invoices/{tracking_id}")
    assert status.json()["invoice"]["submitter"] == _TEST_SUBMITTER_EMAIL
