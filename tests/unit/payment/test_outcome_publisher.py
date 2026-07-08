"""Tests for DaprPaymentOutcomePublisher - Payment's counterpart to
DaprApprovalOutcomePublisher, publishing payment.completed after the saga
reaches a terminal state (COMPLETED or FAILED).
"""

from __future__ import annotations

import json

import grpc
import pytest

from services.payment.outcome_publisher import (
    DaprPaymentOutcomePublisher,
    PaymentOutcomePublisherError,
)
from shared.contracts.models import Decision, PaymentCompletedEvent, PaymentResolution, Route
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
        route=Route.AUTO_APPROVE, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


def _event(resolution: PaymentResolution) -> PaymentCompletedEvent:
    return PaymentCompletedEvent(
        invoice=clean_invoice(), decision=_decision(), resolution=resolution, reason="test reason"
    )


async def test_publish_sends_event_to_payment_completed_topic() -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprPaymentOutcomePublisher(client=fake_client)
    event = _event(PaymentResolution.COMPLETED)

    await publisher.publish(event)

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["pubsub_name"] == "pubsub"
    assert call["topic_name"] == "payment.completed"
    assert call["data_content_type"] == "application/json"
    assert PaymentCompletedEvent.model_validate(json.loads(call["data"])) == event  # type: ignore[arg-type]


@pytest.mark.parametrize("resolution", [PaymentResolution.COMPLETED, PaymentResolution.FAILED])
async def test_publish_always_uses_the_same_topic_regardless_of_resolution(
    resolution: PaymentResolution,
) -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprPaymentOutcomePublisher(client=fake_client)

    await publisher.publish(_event(resolution))

    assert fake_client.calls[0]["topic_name"] == "payment.completed"


async def test_publish_wraps_client_errors_as_payment_outcome_publisher_error() -> None:
    fake_client = _FakeDaprClient(raise_error=True)
    publisher = DaprPaymentOutcomePublisher(client=fake_client)

    with pytest.raises(PaymentOutcomePublisherError):
        await publisher.publish(_event(PaymentResolution.COMPLETED))


def test_construction_does_not_call_factory_eagerly() -> None:
    built: list[_FakeDaprClient] = []

    def factory() -> _FakeDaprClient:
        client = _FakeDaprClient()
        built.append(client)
        return client

    DaprPaymentOutcomePublisher(factory=factory)

    assert built == []
