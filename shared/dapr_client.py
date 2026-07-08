"""Lazy DaprClient construction, shared by every Dapr publisher/repository
in this project.

DaprClient() blocks synchronously in its own constructor for up to
DAPR_HEALTH_TIMEOUT (60s default), retrying a sidecar health check - confirmed
empirically (see PLAN.md's Dapr pub/sub verification notes), not assumed.
Constructing it eagerly in any class's __init__ would hang that class's
default wiring (and so plain pytest/local dev without a sidecar) for up to a
minute. LazyDaprClient defers construction to first actual use (.get()) and
reuses the same instance after that.

This is a holder/wrapper, not a client itself - it does not implement
DaprClient's interface; it only decides when to build one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from dapr.aio.clients import DaprClient

T = TypeVar("T")


class LazyDaprClient(Generic[T]):
    def __init__(self, client: T | None = None, *, factory: Callable[[], T] | None = None) -> None:
        self._client: T | None = client
        self._factory: Callable[[], T] = factory or DaprClient  # type: ignore[assignment]

    def get(self) -> T:
        if self._client is None:
            self._client = self._factory()
        return self._client
