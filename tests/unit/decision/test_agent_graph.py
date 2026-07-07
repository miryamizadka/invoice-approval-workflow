"""Integration tests for the full agent graph: preprocess -> classify -> router.

All tests use MockProvider or a stub - no real network call (ADR-005).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from services.decision.accessors.llm_provider import LLMProviderError
from services.decision.accessors.mock_provider import MockProvider
from services.decision.agent.graph import build_agent_graph
from services.decision.agent.nodes import AgentError
from services.decision.agent.state import AgentState
from services.decision.router.config import DEFAULT_THRESHOLDS
from services.decision.router.router import route_decision
from shared.contracts.models import Recommendation, RecommendationType, Route
from tests.support.decision_fixtures import clean_invoice

VALID_RECOMMENDATION = Recommendation(
    recommendation=RecommendationType.APPROVE,
    confidence=0.9,
    cited_rules=[],
    reasoning="clean",
)


class _FailingProvider:
    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[Any] | None = None,
    ) -> str:
        raise LLMProviderError("provider is unavailable")


@pytest.mark.asyncio
async def test_full_graph_preprocess_to_router_with_mock_provider() -> None:
    provider = MockProvider(response=VALID_RECOMMENDATION.model_dump_json())
    graph = build_agent_graph(provider)
    invoice = clean_invoice()
    initial = AgentState(invoice=invoice, policy="policy text", correlation_id="cid-1")

    result = await graph.ainvoke(initial)
    final_state = AgentState.model_validate(result)

    assert final_state.recommendation == VALID_RECOMMENDATION
    expected_decision = route_decision(
        invoice,
        VALID_RECOMMENDATION,
        is_duplicate=False,
        thresholds=DEFAULT_THRESHOLDS,
        correlation_id="cid-1",
    )
    assert final_state.decision == expected_decision
    assert final_state.decision is not None
    assert final_state.decision.route == Route.AUTO_APPROVE


@pytest.mark.asyncio
async def test_full_graph_propagates_agent_error_instead_of_falling_back_silently() -> None:
    graph = build_agent_graph(_FailingProvider())
    initial = AgentState(invoice=clean_invoice(), policy="policy text", correlation_id="cid-2")
    with pytest.raises(AgentError):
        await graph.ainvoke(initial)


@pytest.mark.asyncio
async def test_full_graph_uses_injected_thresholds_not_the_default() -> None:
    provider = MockProvider(response=VALID_RECOMMENDATION.model_dump_json())
    tiny_ceiling = DEFAULT_THRESHOLDS.model_copy(update={"ceiling": Decimal("1")})
    graph = build_agent_graph(provider, thresholds=tiny_ceiling)
    invoice = clean_invoice()  # $50 total - well above a $1 ceiling
    initial = AgentState(invoice=invoice, policy="policy text", correlation_id="cid-3")

    result = await graph.ainvoke(initial)
    final_state = AgentState.model_validate(result)

    assert final_state.decision is not None
    assert final_state.decision.route == Route.HUMAN_REVIEW
