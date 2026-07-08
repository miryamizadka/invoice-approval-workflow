"""Tests for DaprDecisionOutcomePublisher - Decision's counterpart to
DaprDecisionPublisher, publishing the enriched decision.completed event
after the subscription handler runs the Decider.
"""

from __future__ import annotations

import json

import grpc
import pytest

from services.decision.service.outcome_publisher import (
    DaprDecisionOutcomePublisher,
    DecisionOutcomePublisherError,
)
from shared.contracts.models import Decision, DecisionCompletedEvent, Route
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


def _decision(route: Route) -> Decision:
    return Decision(
        route=route, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


def _event(route: Route) -> DecisionCompletedEvent:
    return DecisionCompletedEvent(invoice=clean_invoice(), decision=_decision(route))


async def test_publish_sends_event_to_decision_completed_topic() -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprDecisionOutcomePublisher(client=fake_client)
    event = _event(Route.AUTO_APPROVE)

    await publisher.publish(event)

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["pubsub_name"] == "pubsub"
    assert call["topic_name"] == "decision.completed"
    assert call["data_content_type"] == "application/json"
    assert DecisionCompletedEvent.model_validate(json.loads(call["data"])) == event  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "route", [Route.AUTO_APPROVE, Route.HUMAN_REVIEW, Route.REJECT, Route.DUPLICATE]
)
async def test_publish_always_uses_the_same_topic_regardless_of_route(route: Route) -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprDecisionOutcomePublisher(client=fake_client)

    await publisher.publish(_event(route))

    assert fake_client.calls[0]["topic_name"] == "decision.completed"


async def test_publish_wraps_client_errors_as_decision_outcome_publisher_error() -> None:
    fake_client = _FakeDaprClient(raise_error=True)
    publisher = DaprDecisionOutcomePublisher(client=fake_client)

    with pytest.raises(DecisionOutcomePublisherError):
        await publisher.publish(_event(Route.AUTO_APPROVE))


def test_construction_does_not_call_factory_eagerly() -> None:
    """Deterministic laziness proof (not timing-based): whatever factory
    would build the real client, it must not be invoked just from
    constructing DaprDecisionOutcomePublisher - only from publish()."""
    built: list[_FakeDaprClient] = []

    def factory() -> _FakeDaprClient:
        client = _FakeDaprClient()
        built.append(client)
        return client

    DaprDecisionOutcomePublisher(factory=factory)

    assert built == []
