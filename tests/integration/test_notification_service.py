"""Integration tests for the Notification Service HTTP API.

Exercises the full chain: HTTP/subscription -> FastAPI -> NotificationService
- with InMemoryNotificationRepository/a fake channel, no real Dapr (same
pattern as test_payment_service.py). No lifespan/startup work exists here
(unlike Payment's budget seeding), so a plain TestClient(app) is fine -
no `with` requirement.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from services.notification.accessors.fake_channel import FakeNotificationChannel
from services.notification.app import create_app
from services.notification.repository import InMemoryNotificationRepository
from shared.contracts.models import ApprovalResolution, Decision, PaymentResolution, Route
from tests.support.decision_fixtures import RAW_FIXTURES, clean_invoice, invoice_from_fixture


def _decision(route: Route = Route.REJECT, correlation_id: str = "corr-1") -> dict[str, Any]:
    return Decision(
        route=route, reason="test reason", triggered_rules=[], correlation_id=correlation_id
    ).model_dump(mode="json")


def _decision_completed_body(route: Route, correlation_id: str = "corr-1") -> dict[str, Any]:
    return {
        "invoice": clean_invoice().model_dump(mode="json"),
        "decision": _decision(route, correlation_id),
        "recommendation": None,
    }


def _approval_completed_body(
    resolution: ApprovalResolution, correlation_id: str = "corr-1"
) -> dict[str, Any]:
    return {
        "invoice": clean_invoice().model_dump(mode="json"),
        "decision": _decision(Route.HUMAN_REVIEW, correlation_id),
        "resolution": resolution.value,
    }


def _payment_completed_body(
    resolution: PaymentResolution, correlation_id: str = "corr-1", reason: str = "test reason"
) -> dict[str, Any]:
    return {
        "invoice": clean_invoice().model_dump(mode="json"),
        "decision": _decision(Route.AUTO_APPROVE, correlation_id),
        "resolution": resolution.value,
        "reason": reason,
    }


def _app(
    repository: InMemoryNotificationRepository | None = None,
    channel: FakeNotificationChannel | None = None,
) -> tuple[TestClient, InMemoryNotificationRepository, FakeNotificationChannel]:
    resolved_repository = repository or InMemoryNotificationRepository()
    resolved_channel = channel or FakeNotificationChannel()
    app = create_app(repository=resolved_repository, channel=resolved_channel)
    return TestClient(app), resolved_repository, resolved_channel


def test_health_check() -> None:
    client, _, _ = _app()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "notification-service"}


def test_decision_completed_reject_posts_and_sends_notification() -> None:
    client, _, channel = _app()

    response = client.post(
        "/events/decision-completed", json={"data": _decision_completed_body(Route.REJECT)}
    )

    assert response.status_code == 200
    assert len(channel.sent) == 1


def test_decision_completed_duplicate_posts_and_sends_notification() -> None:
    client, _, channel = _app()

    client.post(
        "/events/decision-completed", json={"data": _decision_completed_body(Route.DUPLICATE)}
    )

    assert len(channel.sent) == 1
    assert "duplicate" in channel.sent[0][2].lower()


def test_decision_completed_auto_approve_does_not_notify() -> None:
    client, _, channel = _app()

    client.post(
        "/events/decision-completed", json={"data": _decision_completed_body(Route.AUTO_APPROVE)}
    )

    assert channel.sent == []


def test_decision_completed_human_review_does_not_notify() -> None:
    client, _, channel = _app()

    client.post(
        "/events/decision-completed", json={"data": _decision_completed_body(Route.HUMAN_REVIEW)}
    )

    assert channel.sent == []


def test_approval_completed_rejected_posts_and_sends_notification() -> None:
    client, _, channel = _app()

    client.post(
        "/events/approval-completed",
        json={"data": _approval_completed_body(ApprovalResolution.REJECTED)},
    )

    assert len(channel.sent) == 1


def test_approval_completed_approved_does_not_notify() -> None:
    client, _, channel = _app()

    client.post(
        "/events/approval-completed",
        json={"data": _approval_completed_body(ApprovalResolution.APPROVED)},
    )

    assert channel.sent == []


def test_payment_completed_completed_posts_and_sends_notification() -> None:
    client, _, channel = _app()

    client.post(
        "/events/payment-completed",
        json={"data": _payment_completed_body(PaymentResolution.COMPLETED)},
    )

    assert len(channel.sent) == 1


def test_payment_completed_failed_posts_and_sends_notification() -> None:
    client, _, channel = _app()

    client.post(
        "/events/payment-completed",
        json={"data": _payment_completed_body(PaymentResolution.FAILED, reason="gateway declined")},
    )

    assert len(channel.sent) == 1
    assert "gateway declined" in channel.sent[0][2]


def test_redelivery_of_same_event_does_not_send_twice() -> None:
    client, _, channel = _app()
    body = {"data": _payment_completed_body(PaymentResolution.COMPLETED)}

    client.post("/events/payment-completed", json=body)
    client.post("/events/payment-completed", json=body)

    assert len(channel.sent) == 1


def test_get_notification_status_returns_false_for_unknown_tracking_id() -> None:
    client, _, _ = _app()

    response = client.get("/notifications/missing")

    assert response.status_code == 200
    assert response.json() == {"tracking_id": "missing", "notified": False}


def test_get_notification_status_returns_true_after_notification_sent() -> None:
    client, _, _ = _app()
    client.post(
        "/events/payment-completed",
        json={"data": _payment_completed_body(PaymentResolution.COMPLETED)},
    )

    response = client.get("/notifications/corr-1")

    assert response.json() == {"tracking_id": "corr-1", "notified": True}


# --- required-journey tests, fixture-driven --------------------------------


def _raw_fixture(fixture_id: str) -> dict[str, Any]:
    return next(f for f in RAW_FIXTURES if f["id"] == fixture_id)


def test_inv_1007_duplicate_fixture_produces_exactly_one_notification() -> None:
    invoice = invoice_from_fixture(_raw_fixture("INV-1007"))
    client, _, channel = _app()

    client.post(
        "/events/decision-completed",
        json={
            "data": {
                "invoice": invoice.model_dump(mode="json"),
                "decision": _decision(Route.DUPLICATE, "inv-1007-corr"),
                "recommendation": None,
            }
        },
    )

    assert len(channel.sent) == 1
    assert channel.sent[0][1] == "inv-1007-corr"


def test_inv_1015_reject_fixture_produces_exactly_one_notification() -> None:
    invoice = invoice_from_fixture(_raw_fixture("INV-1015"))
    client, _, channel = _app()

    client.post(
        "/events/decision-completed",
        json={
            "data": {
                "invoice": invoice.model_dump(mode="json"),
                "decision": _decision(Route.REJECT, "inv-1015-corr"),
                "recommendation": None,
            }
        },
    )

    assert len(channel.sent) == 1
    assert channel.sent[0][1] == "inv-1015-corr"
