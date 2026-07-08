"""Tests for LazyDaprClient - a holder, not a client itself: wraps either a
pre-injected client (tests) or lazily builds a real DaprClient on first
.get() call and reuses it. Constructing a real DaprClient() blocks for up to
60s (DAPR_HEALTH_TIMEOUT) retrying a sidecar health check if none is
reachable - confirmed empirically (see PLAN.md) - so laziness matters.
"""

from __future__ import annotations

from shared.dapr_client import LazyDaprClient


class _FakeClient:
    pass


def test_get_returns_injected_client_without_calling_factory() -> None:
    fake = _FakeClient()
    factory_calls: list[None] = []

    lazy = LazyDaprClient(client=fake, factory=lambda: factory_calls.append(None) or _FakeClient())  # type: ignore[func-returns-value]

    assert lazy.get() is fake
    assert factory_calls == []


def test_get_builds_via_factory_only_on_first_call() -> None:
    built: list[_FakeClient] = []

    def factory() -> _FakeClient:
        client = _FakeClient()
        built.append(client)
        return client

    lazy: LazyDaprClient[_FakeClient] = LazyDaprClient(factory=factory)

    first = lazy.get()
    second = lazy.get()

    assert len(built) == 1  # factory called exactly once, not once per .get()
    assert first is second is built[0]


def test_construction_without_client_or_factory_does_not_call_real_dapr_client() -> None:
    """Default factory is the real DaprClient, but merely constructing
    LazyDaprClient() must not invoke it - only .get() does."""
    LazyDaprClient()  # must not raise/hang - factory is never called here
