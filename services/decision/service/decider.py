"""Transport-agnostic decision orchestration - no FastAPI/HTTP knowledge here.

decide() is the one thing every transport (this HTTP endpoint today, a Dapr
pub/sub subscriber later) calls. It never knows whether the invoice arrived
over HTTP or an event.
"""

from __future__ import annotations

import logging

from langgraph.graph.state import CompiledStateGraph

from services.decision.accessors.llm_provider import LLMProvider
from services.decision.agent import AgentError, AgentState, build_agent_graph
from services.decision.models import Decision, Invoice
from services.decision.router.config import DEFAULT_THRESHOLDS, AutonomyThresholds
from services.decision.router.router import route_decision
from services.decision.service.policy_loader import load_policy_text


class Decider:
    def __init__(
        self, graph: CompiledStateGraph, thresholds: AutonomyThresholds, policy: str
    ) -> None:
        self._graph = graph
        self._thresholds = thresholds
        self._policy = policy
        self._logger = logging.getLogger(__name__)

    async def decide(
        self, invoice: Invoice, *, correlation_id: str, is_duplicate: bool = False
    ) -> Decision:
        self._logger.info(
            "decision_requested",
            extra={"correlation_id": correlation_id, "invoice_id": invoice.id},
        )
        initial_state = AgentState(
            invoice=invoice,
            policy=self._policy,
            correlation_id=correlation_id,
            is_duplicate=is_duplicate,
        )
        try:
            result = await self._graph.ainvoke(initial_state)
            final_state = AgentState.model_validate(result)
            if final_state.decision is None:
                # Structurally shouldn't happen (RouterNode always sets it) - if
                # it does, it's a graph-wiring bug, not an LLM failure. Deliberately
                # RuntimeError, not AgentError: it must NOT be caught below and
                # silently folded into the human_review fallback - that would hide
                # a real bug behind what looks like a routine LLM outage.
                raise RuntimeError(
                    "Agent graph completed without a Decision - "
                    "graph wiring bug, not an LLM failure."
                )
            decision = final_state.decision
        except AgentError as exc:
            # Fail-clean contract from the agent design: never silently drop to
            # a partial state - log loudly, then reuse the router's own safe
            # path for "no recommendation" (human_review), not a copy of it.
            self._logger.error(
                "agent_failed_falling_back_to_human_review",
                extra={
                    "correlation_id": correlation_id,
                    "invoice_id": invoice.id,
                    "error": str(exc),
                },
            )
            decision = route_decision(
                invoice,
                None,
                is_duplicate=is_duplicate,
                thresholds=self._thresholds,
                correlation_id=correlation_id,
            )
        self._logger.info(
            "decision_completed",
            extra={
                "correlation_id": correlation_id,
                "invoice_id": invoice.id,
                "route": decision.route.value,
            },
        )
        return decision


def build_decider(provider: LLMProvider) -> Decider:
    """Composition, not transport: resolves everything a Decider needs.

    Lives outside create_app() on purpose - the planned Dapr pub/sub
    transport will need a Decider too, and it must not have to import
    FastAPI or reach into app.state to get one. Both transports call this.
    """
    graph = build_agent_graph(provider, DEFAULT_THRESHOLDS)
    return Decider(graph, DEFAULT_THRESHOLDS, load_policy_text())
