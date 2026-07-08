"""Decision's publisher for the final Decision - the counterpart to
services/intake/decision_publisher.py. A Protocol, like LLMProvider, so
tests can inject a stub instead of a real Dapr client.

Single topic (decision.completed), not one per route: Decision doesn't know
or care who's listening (choreography) - a future Approval/Payment service
can filter by the `route` field in the payload, or via Dapr's own
content-based routing on their own subscription, without Decision changing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import grpc

from shared.contracts.models import Decision
from shared.dapr_client import LazyDaprClient

_TOPIC = "decision.completed"


class DecisionOutcomePublisherError(Exception):
    """Raised on any failure publishing a Decision - never caught silently."""


class DecisionOutcomePublisher(Protocol):
    async def publish(self, decision: Decision) -> None: ...


class _DaprPublishClient(Protocol):
    """Same minimal shape as services/intake/decision_publisher.py's -
    lets tests inject a lightweight fake instead of a real DaprClient."""

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str
    ) -> object: ...


class DaprDecisionOutcomePublisher:
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

    async def publish(self, decision: Decision) -> None:
        try:
            await self._dapr.get().publish_event(
                pubsub_name="pubsub",
                topic_name=_TOPIC,
                data=decision.model_dump_json(),
                data_content_type="application/json",
            )
        except grpc.RpcError as exc:
            raise DecisionOutcomePublisherError(
                f"Failed to publish decision.completed: {exc}"
            ) from exc
