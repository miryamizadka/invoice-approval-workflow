"""Unit tests for the agent's individual LangGraph nodes, in isolation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from services.decision.accessors.llm_provider import LLMProviderError
from services.decision.agent.nodes import (
    AgentError,
    ClassifyNode,
    RouterNode,
    build_user_prompt,
    preprocess,
)
from services.decision.agent.state import AgentState
from services.decision.router.config import DEFAULT_THRESHOLDS
from services.decision.router.router import route_decision
from shared.contracts.models import Category, Recommendation, RecommendationType, Route
from tests.support.decision_fixtures import clean_invoice

VALID_RECOMMENDATION = Recommendation(
    recommendation=RecommendationType.APPROVE,
    confidence=0.9,
    cited_rules=[],
    reasoning="looks fine",
)


def _state(**overrides: Any) -> AgentState:
    base: dict[str, Any] = {
        "invoice": clean_invoice(),
        "policy": "policy text",
        "correlation_id": "cid-1",
    }
    base.update(overrides)
    return AgentState(**base)


class _StubProvider:
    def __init__(self, *, response: str | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.last_call: dict[str, Any] | None = None

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[Any] | None = None,
    ) -> str:
        self.last_call = {
            "system_prompt": system_prompt,
            "user_message": user_message,
            "json_mode": json_mode,
            "schema": schema,
        }
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


# --- build_user_prompt (pure, no AgentState needed) --------------------------


def test_build_user_prompt_includes_policy_and_invoice_fields() -> None:
    invoice = clean_invoice()
    prompt = build_user_prompt(invoice, "MEAL-01: up to $75/attendee")
    assert "MEAL-01" in prompt
    assert invoice.vendor in prompt


# --- preprocess --------------------------------------------------------------


def test_preprocess_builds_prepared_prompt_from_invoice_and_policy() -> None:
    state = _state(policy="MEAL-01: up to $75/attendee")
    result = preprocess(state)
    assert "MEAL-01" in result["prepared_prompt"]
    assert state.invoice.vendor in result["prepared_prompt"]
    assert result["prepared_prompt"] == build_user_prompt(state.invoice, state.policy)


def test_preprocess_does_not_touch_other_state_fields() -> None:
    state = _state()
    result = preprocess(state)
    assert set(result.keys()) == {"prepared_prompt"}


# --- ClassifyNode --------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_node_parses_valid_recommendation() -> None:
    provider = _StubProvider(response=VALID_RECOMMENDATION.model_dump_json())
    node = ClassifyNode(provider)
    state = _state(prepared_prompt="prepared text")
    result = await node(state)
    assert result["recommendation"] == VALID_RECOMMENDATION


@pytest.mark.asyncio
async def test_classify_node_records_raw_llm_response_for_audit() -> None:
    raw_json = VALID_RECOMMENDATION.model_dump_json()
    provider = _StubProvider(response=raw_json)
    node = ClassifyNode(provider)
    state = _state(prepared_prompt="prepared text")
    result = await node(state)
    assert result["raw_llm_response"] == raw_json


@pytest.mark.asyncio
async def test_classify_node_requests_recommendation_schema_and_uses_prepared_prompt() -> None:
    provider = _StubProvider(response=VALID_RECOMMENDATION.model_dump_json())
    node = ClassifyNode(provider)
    state = _state(prepared_prompt="prepared text")
    await node(state)
    assert provider.last_call is not None
    assert provider.last_call["schema"] is Recommendation
    assert provider.last_call["user_message"] == "prepared text"


@pytest.mark.asyncio
async def test_classify_node_raises_agent_error_on_provider_failure() -> None:
    provider = _StubProvider(error=LLMProviderError("provider is unavailable"))
    node = ClassifyNode(provider)
    state = _state(prepared_prompt="prepared text")
    with pytest.raises(AgentError, match="LLM call failed"):
        await node(state)


@pytest.mark.asyncio
async def test_classify_node_raises_agent_error_on_schema_invalid_json() -> None:
    provider = _StubProvider(response="not valid json at all")
    node = ClassifyNode(provider)
    state = _state(prepared_prompt="prepared text")
    with pytest.raises(AgentError, match="schema-invalid"):
        await node(state)


@pytest.mark.asyncio
async def test_classify_node_error_message_includes_raw_response_for_debugging() -> None:
    provider = _StubProvider(response="not valid json at all")
    node = ClassifyNode(provider)
    state = _state(prepared_prompt="prepared text")
    with pytest.raises(AgentError, match="not valid json at all"):
        await node(state)


# --- RouterNode ------------------------------------------------------------------


def test_router_node_reuses_existing_route_decision() -> None:
    invoice = clean_invoice()
    state = _state(invoice=invoice, recommendation=VALID_RECOMMENDATION)
    node = RouterNode(DEFAULT_THRESHOLDS)
    result = node(state)
    expected = route_decision(
        invoice,
        VALID_RECOMMENDATION,
        is_duplicate=False,
        thresholds=DEFAULT_THRESHOLDS,
        correlation_id="cid-1",
    )
    assert result["decision"] == expected


def test_router_node_uses_injected_thresholds_not_the_default() -> None:
    # $300, category OTHER (no cap): over the default $250 ceiling, under a custom $500 one.
    invoice = clean_invoice(category=Category.OTHER, attendees=None, total=Decimal("300.00"))
    custom_thresholds = DEFAULT_THRESHOLDS.model_copy(update={"ceiling": Decimal("500")})
    state = _state(invoice=invoice, recommendation=VALID_RECOMMENDATION)

    default_result = RouterNode(DEFAULT_THRESHOLDS)(state)
    custom_result = RouterNode(custom_thresholds)(state)

    assert default_result["decision"].route == Route.HUMAN_REVIEW
    assert custom_result["decision"].route == Route.AUTO_APPROVE
