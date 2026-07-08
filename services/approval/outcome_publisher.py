"""Approval's publisher for the human resolution outcome - the counterpart to
services/decision/service/outcome_publisher.py. A Protocol, like
DecisionOutcomePublisher, so tests can inject a stub instead of a real Dapr
client.

Single topic (approval.completed), not one per resolution: Approval doesn't
know or care who's listening (choreography, same decision already made for
decision.completed) - a future Payment/Notification service filters by the
`resolution` field in the payload.

request_info (send-back to WAITING_INFO) deliberately does not publish
anything here - per ADR-003, once escalated the human owns the decision;
there is no consumer waiting for a "still pending" signal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import grpc

from shared.contracts.models import ApprovalCompletedEvent
from shared.dapr_client import LazyDaprClient

_TOPIC = "approval.completed"


class ApprovalOutcomePublisherError(Exception):
    """Raised on any failure publishing an approval outcome - never caught silently."""


class ApprovalOutcomePublisher(Protocol):
    async def publish(self, event: ApprovalCompletedEvent) -> None: ...


class _DaprPublishClient(Protocol):
    """Same minimal shape as DaprDecisionOutcomePublisher's - lets tests
    inject a lightweight fake instead of a real DaprClient."""

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str
    ) -> object: ...


class DaprApprovalOutcomePublisher:
    """Lazy client construction, same reasoning as DaprDecisionOutcomePublisher:
    a real DaprClient() blocks for up to 60s retrying a sidecar health check
    if none is reachable."""

    def __init__(
        self,
        *,
        client: _DaprPublishClient | None = None,
        factory: Callable[[], _DaprPublishClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprPublishClient](client, factory=factory)

    async def publish(self, event: ApprovalCompletedEvent) -> None:
        try:
            await self._dapr.get().publish_event(
                pubsub_name="pubsub",
                topic_name=_TOPIC,
                data=event.model_dump_json(),
                data_content_type="application/json",
            )
        except grpc.RpcError as exc:
            raise ApprovalOutcomePublisherError(
                f"Failed to publish approval.completed: {exc}"
            ) from exc
