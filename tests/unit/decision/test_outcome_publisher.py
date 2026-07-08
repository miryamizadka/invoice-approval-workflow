"""Tests for DaprDecisionOutcomePublisher - Decision's counterpart to
DaprDecisionPublisher, publishing the final Decision to decision.completed
after the subscription handler runs the Decider.
"""

from __future__ import annotations

import json
import time

import grpc
import pytest

from services.decision.service.outcome_publisher import (
    DaprDecisionOutcomePublisher,
    DecisionOutcomePublisherError,
)
from shared.contracts.models import Decision, Route


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


def _decision(route: Route) -> Decision:
    return Decision(
        route=route, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


async def test_publish_sends_decision_to_decision_completed_topic() -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprDecisionOutcomePublisher(client=fake_client)
    decision = _decision(Route.AUTO_APPROVE)

    await publisher.publish(decision)

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["pubsub_name"] == "pubsub"
    assert call["topic_name"] == "decision.completed"
    assert call["data_content_type"] == "application/json"
    assert Decision.model_validate(json.loads(call["data"])) == decision  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "route", [Route.AUTO_APPROVE, Route.HUMAN_REVIEW, Route.REJECT, Route.DUPLICATE]
)
async def test_publish_always_uses_the_same_topic_regardless_of_route(route: Route) -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprDecisionOutcomePublisher(client=fake_client)

    await publisher.publish(_decision(route))

    assert fake_client.calls[0]["topic_name"] == "decision.completed"


async def test_publish_wraps_client_errors_as_decision_outcome_publisher_error() -> None:
    fake_client = _FakeDaprClient(raise_error=True)
    publisher = DaprDecisionOutcomePublisher(client=fake_client)

    with pytest.raises(DecisionOutcomePublisherError):
        await publisher.publish(_decision(Route.AUTO_APPROVE))


def test_construction_without_injected_client_does_not_block() -> None:
    start = time.monotonic()
    DaprDecisionOutcomePublisher()
    assert time.monotonic() - start < 1.0
