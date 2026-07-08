"""Tests for DaprDecisionPublisher - the pub/sub replacement for
HttpDecisionServiceClient. Uses a fake matching the minimal _DaprPublishClient
Protocol, never a real DaprClient - constructing a real one blocks for up to
60s (DAPR_HEALTH_TIMEOUT) retrying a sidecar health check that doesn't exist
in a plain test run (confirmed empirically, not assumed).
"""

from __future__ import annotations

import json
from typing import Any

import grpc
import pytest

from services.intake.decision_publisher import DaprDecisionPublisher, DecisionPublisherError
from shared.contracts.models import InvoiceSubmittedEvent
from tests.support.decision_fixtures import clean_invoice


class _FakeDaprClient:
    def __init__(self, *, raise_error: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
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


async def test_publish_sends_invoice_submitted_event_to_pubsub_component() -> None:
    fake_client = _FakeDaprClient()
    publisher = DaprDecisionPublisher(client=fake_client)
    invoice = clean_invoice()

    await publisher.publish(invoice, correlation_id="corr-1")

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["pubsub_name"] == "pubsub"
    assert call["topic_name"] == "invoice.submitted"
    assert call["data_content_type"] == "application/json"
    event = InvoiceSubmittedEvent.model_validate(json.loads(call["data"]))
    assert event.correlation_id == "corr-1"
    assert event.invoice == invoice


async def test_publish_wraps_client_errors_as_decision_publisher_error() -> None:
    fake_client = _FakeDaprClient(raise_error=True)
    publisher = DaprDecisionPublisher(client=fake_client)

    with pytest.raises(DecisionPublisherError):
        await publisher.publish(clean_invoice(), correlation_id="corr-1")


def test_construction_does_not_call_factory_eagerly() -> None:
    """Deterministic laziness proof (not timing-based): whatever factory
    would build the real client, it must not be invoked just from
    constructing DaprDecisionPublisher - only from publish()."""
    built: list[_FakeDaprClient] = []

    def factory() -> _FakeDaprClient:
        client = _FakeDaprClient()
        built.append(client)
        return client

    DaprDecisionPublisher(factory=factory)

    assert built == []
