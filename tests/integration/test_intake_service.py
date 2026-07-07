"""Integration tests for the Intake Service HTTP API.

Exercises HTTP -> IntakeService -> InMemoryInvoiceRepository, with a stub
DecisionServiceClient - no real network call to Decision Service.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.intake.app import create_app
from services.intake.decision_client import DecisionClientError, DecisionServiceClient
from services.intake.repository import InMemoryInvoiceRepository, InvoiceRepository
from shared.contracts.models import Decision, Invoice, Route


class _StubDecisionClient:
    def __init__(
        self, *, decision: Decision | None = None, error: Exception | None = None
    ) -> None:
        self._decision = decision
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def decide(
        self, invoice: Invoice, *, correlation_id: str, is_duplicate: bool = False
    ) -> Decision:
        self.calls.append(
            {"invoice": invoice, "correlation_id": correlation_id, "is_duplicate": is_duplicate}
        )
        if self._error is not None:
            raise self._error
        assert self._decision is not None
        return self._decision


def _decision(route: Route = Route.AUTO_APPROVE, correlation_id: str = "cid") -> Decision:
    return Decision(
        route=route, reason="stub decision", triggered_rules=[], correlation_id=correlation_id
    )


def _build_app(
    decision_client: DecisionServiceClient, repository: InvoiceRepository | None = None
) -> FastAPI:
    return create_app(
        repository=repository or InMemoryInvoiceRepository(), decision_client=decision_client
    )


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


# --- (a) submit returns tracking id immediately -------------------------------


def test_submit_returns_tracking_id_immediately() -> None:
    app = _build_app(_StubDecisionClient(decision=_decision()))
    client = TestClient(app)

    response = client.post("/invoices", json=_invoice_body())

    assert response.status_code == 202
    assert response.json()["tracking_id"]
    assert response.headers["X-Correlation-Id"] == response.json()["tracking_id"]


# --- (b) status reflects state after background processing -------------------


def test_status_reflects_completed_after_background_processing() -> None:
    app = _build_app(_StubDecisionClient(decision=_decision(route=Route.AUTO_APPROVE)))
    client = TestClient(app)

    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]
    status = client.get(f"/invoices/{tracking_id}")

    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "completed"
    assert body["decision"]["route"] == Route.AUTO_APPROVE.value


# --- (c) duplicate detected: is_duplicate=True passed to Decision -------------


def test_duplicate_submission_passes_is_duplicate_true() -> None:
    stub = _StubDecisionClient(decision=_decision())
    app = _build_app(stub)
    client = TestClient(app)
    body = _invoice_body()

    client.post("/invoices", json=body)
    client.post("/invoices", json=body)

    assert len(stub.calls) == 2
    assert stub.calls[0]["is_duplicate"] is False
    assert stub.calls[1]["is_duplicate"] is True


# --- (d) invalid invoice -> 422 -----------------------------------------------


def test_invalid_invoice_returns_422() -> None:
    app = _build_app(_StubDecisionClient(decision=_decision()))
    client = TestClient(app)

    response = client.post("/invoices", json={"id": "TEST-0002"})

    assert response.status_code == 422


# --- (e) dedup_key not exposed in API response --------------------------------


def test_dedup_key_not_exposed_in_status_response() -> None:
    app = _build_app(_StubDecisionClient(decision=_decision()))
    client = TestClient(app)

    tracking_id = client.post("/invoices", json=_invoice_body()).json()["tracking_id"]
    status = client.get(f"/invoices/{tracking_id}")

    assert "dedup_key" not in status.json()


# --- (f) unknown tracking_id -> 404 -------------------------------------------


def test_unknown_tracking_id_returns_404() -> None:
    app = _build_app(_StubDecisionClient(decision=_decision()))
    client = TestClient(app)

    response = client.get("/invoices/does-not-exist")

    assert response.status_code == 404


# --- (g) DecisionServiceClient failure -> failed, not a crash, no raw leak ----


def test_decision_client_failure_marks_status_failed_without_leaking_raw_error() -> None:
    error = DecisionClientError("Decision Service call failed: connect to http://internal-host:8001")
    app = _build_app(_StubDecisionClient(error=error))
    client = TestClient(app)

    submit = client.post("/invoices", json=_invoice_body())
    assert submit.status_code == 202
    tracking_id = submit.json()["tracking_id"]

    status = client.get(f"/invoices/{tracking_id}")

    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "failed"
    assert "internal-host" not in (body.get("reason") or "")


# --- (h) health check ----------------------------------------------------------


def test_health_check() -> None:
    app = _build_app(_StubDecisionClient(decision=_decision()))
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "intake-service"}
