"""Tests for DaprApprovalOutcomePublisher - Approval's counterpart to
DaprDecisionOutcomePublisher, publishing approval.completed after a human
resolves an item (approve/reject).
"""

from __future__ import annotations

import json

import grpc
import pytest

from services.approval.outcome_publisher import (
    ApprovalOutcomePublisherError,
    DaprApprovalOutcomePublisher,
)
from shared.contracts.models import ApprovalCompletedEvent, ApprovalResolution, Decision, Route
from tests.support.decision_fixtures import clean_invoice


class _FakeDaprClient:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._raise_error = raise_error

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str
    ) -> None:
        if self._raise_error:
            raise grpc.RpcError()
        self.calls.append(
            {
                "pubsub_name": pubsub_name,
                "topic_name": topic_name,
                "data": data,
                "data_content_type": data_content_type,
            }
        )


def _decision() -> Decision:
    return Decision(
        route=Route.HUMAN_REVIEW, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


def _event(resolution: ApprovalResolution) -> ApprovalCompletedEvent:
    return ApprovalCompletedEvent(
        invoice=clean_invoice(), decision=_decision(), resolution=resolution
    )


async def test_publish_sends_event_to_approval_completed_topic() -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprApprovalOutcomePublisher(client=fake_client)
    event = _event(ApprovalResolution.APPROVED)

    await publisher.publish(event)

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["pubsub_name"] == "pubsub"
    assert call["topic_name"] == "approval.completed"
    assert call["data_content_type"] == "application/json"
    assert ApprovalCompletedEvent.model_validate(json.loads(call["data"])) == event  # type: ignore[arg-type]


@pytest.mark.parametrize("resolution", [ApprovalResolution.APPROVED, ApprovalResolution.REJECTED])
async def test_publish_always_uses_the_same_topic_regardless_of_resolution(
    resolution: ApprovalResolution,
) -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprApprovalOutcomePublisher(client=fake_client)

    await publisher.publish(_event(resolution))

    assert fake_client.calls[0]["topic_name"] == "approval.completed"


async def test_publish_wraps_client_errors_as_approval_outcome_publisher_error() -> None:
    fake_client = _FakeDaprClient(raise_error=True)
    publisher = DaprApprovalOutcomePublisher(client=fake_client)

    with pytest.raises(ApprovalOutcomePublisherError):
        await publisher.publish(_event(ApprovalResolution.APPROVED))


def test_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprClient] = []

    def factory() -> _FakeDaprClient:
        client = _FakeDaprClient()
        built.append(client)
        return client

    DaprApprovalOutcomePublisher(factory=factory)

    assert built == []
