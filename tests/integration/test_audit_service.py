"""Integration tests for the Audit Service HTTP API (F9).

Exercises HTTP -> AuditService -> InMemoryAuditRepository - no real Postgres,
no real Dapr sidecar (same posture as test_notification_service.py). The
three subscription routes are exercised directly via TestClient, POSTing a
CloudEvent-shaped body (just the `data` field) to simulate what the Dapr
sidecar would deliver.

Uses `with TestClient(app) as client:` - create_app()'s lifespan hook
(ensure_schema()) only runs inside the context manager, same reason as
test_payment_service.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.audit.app import create_app
from services.audit.repository import AuditRepository, InMemoryAuditRepository
from shared.contracts.models import ApprovalResolution, PaymentResolution, Route
from tests.support.event_fixtures import (
    approval_completed_event,
    decision_completed_event,
    payment_completed_event,
)


def _build_app(repository: AuditRepository | None = None) -> FastAPI:
    return create_app(repository=repository or InMemoryAuditRepository())


def _post_decision_completed(client: TestClient, **overrides: Any) -> Any:
    event = decision_completed_event(**overrides)
    return client.post("/events/decision-completed", json={"data": event.model_dump(mode="json")})


def _post_approval_completed(client: TestClient, **overrides: Any) -> Any:
    event = approval_completed_event(**overrides)
    return client.post("/events/approval-completed", json={"data": event.model_dump(mode="json")})


def _post_payment_completed(client: TestClient, **overrides: Any) -> Any:
    event = payment_completed_event(**overrides)
    return client.post("/events/payment-completed", json={"data": event.model_dump(mode="json")})


# --- decision.completed builds the trail -------------------------------------


def test_decision_completed_creates_a_retrievable_trail() -> None:
    app = _build_app()
    with TestClient(app) as client:
        response = _post_decision_completed(client, route=Route.AUTO_APPROVE)
        trail = client.get("/audit/corr-1")

    assert response.status_code == 200
    assert trail.status_code == 200
    body = trail.json()
    assert body["route"] == Route.AUTO_APPROVE.value
    assert body["tracking_id"] == "corr-1"


# --- full journey accumulates one trail --------------------------------------


def test_full_journey_accumulates_into_one_trail() -> None:
    # Uses the same Decision object across all three events - what Approval/
    # Payment actually relay in production (they never invent their own
    # Decision). The shared event_fixtures helpers otherwise default to their
    # own hardcoded routes per event type, which would be unrealistic here.
    app = _build_app()
    decision_event = decision_completed_event(route=Route.HUMAN_REVIEW)
    with TestClient(app) as client:
        client.post(
            "/events/decision-completed", json={"data": decision_event.model_dump(mode="json")}
        )
        _post_approval_completed(
            client, resolution=ApprovalResolution.APPROVED, decision=decision_event.decision
        )
        _post_payment_completed(
            client, resolution=PaymentResolution.COMPLETED, decision=decision_event.decision
        )
        trail = client.get("/audit/corr-1").json()

    assert trail["route"] == Route.HUMAN_REVIEW.value
    assert trail["approval_resolution"] == ApprovalResolution.APPROVED.value
    assert trail["payment_resolution"] == PaymentResolution.COMPLETED.value


# --- unknown tracking_id -> 404 -----------------------------------------------


def test_unknown_tracking_id_returns_404() -> None:
    app = _build_app()
    with TestClient(app) as client:
        response = client.get("/audit/does-not-exist")

    assert response.status_code == 404


# --- health check --------------------------------------------------------------


def test_health_check() -> None:
    app = _build_app()
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "audit-service"}
