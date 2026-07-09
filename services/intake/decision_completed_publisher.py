"""Intake's publisher for decision.completed on a known-duplicate short-circuit
- the counterpart to services/decision/service/outcome_publisher.py.

A known duplicate never reaches Decision (running the agent for it would be
pure waste - see IntakeService.process()), so decision.completed would
otherwise never fire for it, leaving Notification (and any future consumer)
unable to react. This publisher lets Intake publish that same event directly
for this one case. Two services publishing to the same topic is still valid
choreography here: every consumer (Payment, Notification) filters by the
`route` field in the payload, never by which service published it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import grpc

from shared.contracts.models import DecisionCompletedEvent
from shared.dapr_client import LazyDaprClient

_TOPIC = "decision.completed"


class DecisionCompletedPublisherError(Exception):
    """Raised on any failure publishing decision.completed - never caught silently."""


class DecisionCompletedPublisher(Protocol):
    async def publish(self, event: DecisionCompletedEvent) -> None: ...


class _DaprPublishClient(Protocol):
    """Same minimal shape as services/intake/decision_publisher.py's -
    lets tests inject a lightweight fake instead of a real DaprClient."""

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str
    ) -> object: ...


class DaprDecisionCompletedPublisher:
    """Lazy client construction, same reasoning as DaprDecisionPublisher: a
    real DaprClient() blocks for up to 60s retrying a sidecar health check
    if none is reachable - confirmed empirically. LazyDaprClient
    (shared/dapr_client.py) builds it once, on first publish() call, and
    reuses it after that."""

    def __init__(
        self,
        *,
        client: _DaprPublishClient | None = None,
        factory: Callable[[], _DaprPublishClient] | None = None,
    ) -> None:
        self._dapr = LazyDaprClient[_DaprPublishClient](client, factory=factory)

    async def publish(self, event: DecisionCompletedEvent) -> None:
        try:
            await self._dapr.get().publish_event(
                pubsub_name="pubsub",
                topic_name=_TOPIC,
                data=event.model_dump_json(),
                data_content_type="application/json",
            )
        except grpc.RpcError as exc:
            raise DecisionCompletedPublisherError(
                f"Failed to publish decision.completed: {exc}"
            ) from exc
