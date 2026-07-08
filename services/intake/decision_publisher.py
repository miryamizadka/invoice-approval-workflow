"""Intake's publisher for Decision Service - a Protocol, like DecisionServiceClient.

Dapr pub/sub, not HTTP: Intake publishes invoice.submitted and does not wait
for a reply - Decision's own subscription handler (services/decision/service/app.py)
runs the Decider and publishes the outcome back to decision.completed, which
IntakeService.complete() picks up. See services/intake/decision_client.py for
the older synchronous HTTP alternative, kept but no longer wired in by default.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import grpc

from shared.contracts.models import Invoice, InvoiceSubmittedEvent
from shared.dapr_client import LazyDaprClient


class DecisionPublisherError(Exception):
    """Raised on any failure publishing invoice.submitted - never caught silently."""


class DecisionPublisher(Protocol):
    async def publish(self, invoice: Invoice, *, correlation_id: str) -> None: ...


class _DaprPublishClient(Protocol):
    """The minimal shape DaprDecisionPublisher needs from a Dapr client -
    lets tests inject a lightweight fake instead of a real DaprClient, which
    blocks for up to 60s retrying a sidecar health check if none is running."""

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str
    ) -> object: ...


class DaprDecisionPublisher:
    """Lazy client construction is deliberate, not an oversight: a real
    DaprClient() calls DaprHealth.wait_for_sidecar() synchronously in its own
    __init__, blocking for up to DAPR_HEALTH_TIMEOUT (60s default) if no
    sidecar is reachable - confirmed empirically, not assumed. Building it
    eagerly here would make create_app()'s default wiring (and so plain
    pytest/local dev without Docker) hang. LazyDaprClient (shared/dapr_client.py)
    builds it once, on first publish() call, and reuses it after that."""

    def __init__(
        self,
        *,
        client: _DaprPublishClient | None = None,
        factory: Callable[[], _DaprPublishClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprPublishClient](client, factory=factory)

    async def publish(self, invoice: Invoice, *, correlation_id: str) -> None:
        event = InvoiceSubmittedEvent(invoice=invoice, correlation_id=correlation_id)
        try:
            await self._dapr.get().publish_event(
                pubsub_name="pubsub",
                topic_name="invoice.submitted",
                data=event.model_dump_json(),
                data_content_type="application/json",
            )
        except grpc.RpcError as exc:
            # dapr.clients.exceptions.DaprGrpcError (what publish_event actually
            # raises) is itself a grpc.RpcError subclass - catching the base
            # class here also covers any lower-level RpcError that isn't
            # wrapped, and is trivially fakeable in tests (grpc.RpcError() is a
            # plain, argument-free exception; DaprGrpcError requires a real
            # gRPC call object to construct).
            raise DecisionPublisherError(f"Failed to publish invoice.submitted: {exc}") from exc
