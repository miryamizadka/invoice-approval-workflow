"""Assembles the Decision agent's LangGraph: preprocess -> classify -> router."""

from __future__ import annotations

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from services.decision.accessors.llm_provider import LLMProvider
from services.decision.agent.nodes import ClassifyNode, RouterNode, preprocess
from services.decision.agent.state import AgentState
from services.decision.router.config import DEFAULT_THRESHOLDS, AutonomyThresholds


def build_agent_graph(
    provider: LLMProvider, thresholds: AutonomyThresholds = DEFAULT_THRESHOLDS
) -> CompiledStateGraph:
    """Wire the three nodes with the given dependencies injected.

    DEFAULT_THRESHOLDS is only the parameter's default value, not something
    baked into node logic - callers (tests, or later M13 config loading)
    can pass their own AutonomyThresholds without touching this function.
    """
    graph: StateGraph = StateGraph(AgentState)
    graph.add_node("preprocess", preprocess)
    graph.add_node("classify", ClassifyNode(provider))
    graph.add_node("router", RouterNode(thresholds))
    graph.set_entry_point("preprocess")
    graph.add_edge("preprocess", "classify")
    graph.add_edge("classify", "router")
    graph.add_edge("router", END)
    return graph.compile()
