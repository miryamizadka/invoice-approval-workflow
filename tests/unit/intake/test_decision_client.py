"""Tests for HttpDecisionServiceClient - kept as a standalone synchronous
alternative to DecisionPublisher (see its module docstring), no longer
IntakeService's default transport, but still a real, tested implementation.
Uses httpx.MockTransport - no real network call, no real Decision Service.
"""

from __future__ import annotations

import httpx
import pytest

from services.intake.decision_client import DecisionClientError, HttpDecisionServiceClient
from shared.contracts.models import Decision, Route
from tests.support.decision_fixtures import clean_invoice


def _client_returning(handler) -> HttpDecisionServiceClient:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return HttpDecisionServiceClient("http://decision:8001", client=async_client)


async def test_decide_posts_invoice_and_returns_decision() -> None:
    decision = Decision(
        route=Route.AUTO_APPROVE, reason="ok", triggered_rules=[], correlation_id="corr-1"
    )
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=decision.model_dump(mode="json"))

    client = _client_returning(handler)

    result = await client.decide(clean_invoice(), correlation_id="corr-1")

    assert result == decision
    request = captured["request"]
    assert request.url.path == "/decisions"
    assert request.headers["X-Correlation-Id"] == "corr-1"
    assert request.url.params["is_duplicate"] == "false"


async def test_decide_passes_is_duplicate_true() -> None:
    decision = Decision(
        route=Route.DUPLICATE, reason="dup", triggered_rules=["GLOBAL-DUP"], correlation_id="c"
    )
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json=decision.model_dump(mode="json"))

    client = _client_returning(handler)

    await client.decide(clean_invoice(), correlation_id="c", is_duplicate=True)

    assert captured["request"].url.params["is_duplicate"] == "true"


async def test_decide_raises_decision_client_error_on_http_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal_error"})

    client = _client_returning(handler)

    with pytest.raises(DecisionClientError):
        await client.decide(clean_invoice(), correlation_id="corr-1")
