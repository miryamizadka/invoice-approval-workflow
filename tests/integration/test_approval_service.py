"""Integration tests for the Approval Service HTTP API.

Exercises the full chain: HTTP/subscription -> FastAPI -> ApprovalService -
with InMemoryApprovalRepository/a stub publisher, no real Dapr (same pattern
as test_decision_service.py).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from services.approval.app import create_app
from services.approval.models import ApprovalStatus
from services.approval.repository import ApprovalRepository, InMemoryApprovalRepository
from shared.contracts.models import ApprovalCompletedEvent, Decision, Route
from tests.support.decision_fixtures import clean_invoice


class _StubOutcomePublisher:
    def __init__(self) -> None:
        self.published: list[ApprovalCompletedEvent] = []

    async def publish(self, event: ApprovalCompletedEvent) -> None:
        self.published.append(event)


def _decision(route: Route = Route.HUMAN_REVIEW, correlation_id: str = "corr-1") -> dict[str, Any]:
    return Decision(
        route=route, reason="test reason", triggered_rules=[], correlation_id=correlation_id
    ).model_dump(mode="json")


def _decision_completed_event_body(
    route: Route = Route.HUMAN_REVIEW, correlation_id: str = "corr-1"
) -> dict[str, Any]:
    return {
        "invoice": clean_invoice().model_dump(mode="json"),
        "decision": _decision(route, correlation_id),
        "recommendation": None,
    }


def _post_decision_completed(client: TestClient, **overrides: Any) -> Any:
    return client.post(
        "/events/decision-completed",
        json={"data": _decision_completed_event_body(**overrides)},
    )


def _app(
    repository: ApprovalRepository | None = None, publisher: _StubOutcomePublisher | None = None
) -> tuple[TestClient, ApprovalRepository, _StubOutcomePublisher]:
    resolved_repository = repository or InMemoryApprovalRepository()
    resolved_publisher = publisher or _StubOutcomePublisher()
    app = create_app(repository=resolved_repository, publisher=resolved_publisher)
    return TestClient(app), resolved_repository, resolved_publisher


def test_health_check() -> None:
    client, _, _ = _app()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "approval-service"}


def test_decision_completed_event_for_human_review_appears_in_queue() -> None:
    client, _, _ = _app()

    response = _post_decision_completed(client)

    assert response.status_code == 200
    queue = client.get("/approvals").json()
    assert len(queue) == 1
    assert queue[0]["tracking_id"] == "corr-1"
    assert queue[0]["status"] == ApprovalStatus.PENDING.value


def test_decision_completed_event_for_other_routes_does_not_reach_queue() -> None:
    client, _, _ = _app()

    _post_decision_completed(client, route=Route.AUTO_APPROVE, correlation_id="corr-auto")

    assert client.get("/approvals").json() == []


def test_get_single_approval_returns_200() -> None:
    client, _, _ = _app()
    _post_decision_completed(client)

    response = client.get("/approvals/corr-1")

    assert response.status_code == 200
    assert response.json()["tracking_id"] == "corr-1"


def test_get_single_approval_returns_404_when_unknown() -> None:
    client, _, _ = _app()

    response = client.get("/approvals/missing")

    assert response.status_code == 404


def test_approve_updates_status_and_publishes() -> None:
    client, _, publisher = _app()
    _post_decision_completed(client)

    response = client.post("/approvals/corr-1/approve")

    assert response.status_code == 200
    assert response.json()["status"] == ApprovalStatus.APPROVED.value
    assert client.get("/approvals/corr-1").json()["status"] == ApprovalStatus.APPROVED.value
    assert len(publisher.published) == 1


def test_approve_twice_returns_409_and_does_not_publish_twice() -> None:
    client, _, publisher = _app()
    _post_decision_completed(client)
    client.post("/approvals/corr-1/approve")

    response = client.post("/approvals/corr-1/approve")

    assert response.status_code == 409
    assert len(publisher.published) == 1


def test_approve_unknown_tracking_id_returns_404() -> None:
    client, _, _ = _app()

    response = client.post("/approvals/missing/approve")

    assert response.status_code == 404


def test_reject_updates_status_and_publishes() -> None:
    client, _, publisher = _app()
    _post_decision_completed(client)

    response = client.post("/approvals/corr-1/reject")

    assert response.status_code == 200
    assert response.json()["status"] == ApprovalStatus.REJECTED.value
    assert len(publisher.published) == 1


def test_request_info_keeps_item_in_queue_without_publishing() -> None:
    client, _, publisher = _app()
    _post_decision_completed(client)

    response = client.post("/approvals/corr-1/request-info")

    assert response.status_code == 200
    assert response.json()["status"] == ApprovalStatus.WAITING_INFO.value
    queue = client.get("/approvals").json()
    assert len(queue) == 1
    assert queue[0]["status"] == ApprovalStatus.WAITING_INFO.value
    assert publisher.published == []
