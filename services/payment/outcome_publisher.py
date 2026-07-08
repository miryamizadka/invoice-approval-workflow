"""Payment's publisher for the saga's terminal outcome - the counterpart to
services/approval/outcome_publisher.py. A Protocol, like
ApprovalOutcomePublisher, so tests can inject a stub instead of a real Dapr
client.

Single topic (payment.completed), not one per resolution: Payment doesn't
know or care who's listening (choreography, same decision already made for
decision.completed/approval.completed) - a future Notification service
filters by the `resolution` field in the payload.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import grpc

from shared.contracts.models import PaymentCompletedEvent
from shared.dapr_client import LazyDaprClient

_TOPIC = "payment.completed"


class PaymentOutcomePublisherError(Exception):
    """Raised on any failure publishing a payment outcome - never caught silently."""


class PaymentOutcomePublisher(Protocol):
    async def publish(self, event: PaymentCompletedEvent) -> None: ...


class _DaprPublishClient(Protocol):
    """Same minimal shape as DaprApprovalOutcomePublisher's - lets tests
    inject a lightweight fake instead of a real DaprClient."""

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str
    ) -> object: ...


class DaprPaymentOutcomePublisher:
    """Lazy client construction, same reasoning as DaprApprovalOutcomePublisher:
    a real DaprClient() blocks for up to 60s retrying a sidecar health check
    if none is reachable."""

    def __init__(
        self,
        *,
        client: _DaprPublishClient | None = None,
        factory: Callable[[], _DaprPublishClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprPublishClient](client, factory=factory)

    async def publish(self, event: PaymentCompletedEvent) -> None:
        try:
            await self._dapr.get().publish_event(
                pubsub_name="pubsub",
                topic_name=_TOPIC,
                data=event.model_dump_json(),
                data_content_type="application/json",
            )
        except grpc.RpcError as exc:
            raise PaymentOutcomePublisherError(
                f"Failed to publish payment.completed: {exc}"
            ) from exc
