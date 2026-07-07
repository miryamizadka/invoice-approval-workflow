"""The Decision agent's three LangGraph nodes: preprocess, classify, router.

Fail-clean contract: ClassifyNode raises AgentError on any LLM/parsing
failure - it never falls back to recommendation=None silently inside the
graph. The caller (the future Decision Service orchestration layer) is
responsible for catching AgentError, logging/alerting it clearly, and then
calling route_decision(invoice, recommendation=None, ...) directly to reach
the same safe human_review outcome the router already provides for a
missing recommendation. This keeps "no recommendation" unambiguous for the
audit trail (F9): it always means "the agent didn't produce one", not
sometimes-a-failure-sometimes-not, and keeps operational LLM failures
visible rather than silently absorbed into a growing review queue.
"""

from __future__ import annotations

from pydantic import ValidationError

from services.decision.accessors.llm_provider import LLMProvider, LLMProviderError
from services.decision.agent.prompts import SYSTEM_PROMPT
from services.decision.agent.state import AgentState
from services.decision.models import Decision, Invoice, Recommendation
from services.decision.router.config import AutonomyThresholds
from services.decision.router.router import route_decision


class AgentError(Exception):
    """Engine-layer fail-clean error - never silently swallowed."""


def build_user_prompt(invoice: Invoice, policy: str) -> str:
    """Pure prompt-building logic, testable without constructing an AgentState."""
    return f"Policy:\n{policy}\n\nInvoice to evaluate:\n{invoice.model_dump_json(indent=2)}"


def preprocess(state: AgentState) -> dict[str, str]:
    """Build the user-facing prompt text from the invoice and policy."""
    return {"prepared_prompt": build_user_prompt(state.invoice, state.policy)}


class ClassifyNode:
    """Owns the provider dependency (DI) - a class, not a bare closure, so
    it's trivially constructible and testable outside the graph."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def __call__(self, state: AgentState) -> dict[str, str | Recommendation]:
        assert state.prepared_prompt is not None  # preprocess always runs first
        try:
            raw = await self._provider.complete(
                SYSTEM_PROMPT, state.prepared_prompt, schema=Recommendation
            )
        except LLMProviderError as exc:
            raise AgentError(f"LLM call failed during classification: {exc}") from exc
        try:
            recommendation = Recommendation.model_validate_json(raw)
        except ValidationError as exc:
            # Defense in depth: strict mode should guarantee this never fires.
            # The raw text is included here (not just in state) since the node
            # raises before returning any state update in this branch.
            raise AgentError(
                f"LLM returned a schema-invalid recommendation: {exc}. Raw response: {raw!r}"
            ) from exc
        return {"raw_llm_response": raw, "recommendation": recommendation}


class RouterNode:
    """Owns the thresholds dependency (DI) - same pattern as ClassifyNode,
    not a hardcoded import of DEFAULT_THRESHOLDS. M13 (external config)
    means the caller decides which thresholds this graph run enforces."""

    def __init__(self, thresholds: AutonomyThresholds) -> None:
        self._thresholds = thresholds

    def __call__(self, state: AgentState) -> dict[str, Decision]:
        assert state.recommendation is not None  # classify always runs first
        decision = route_decision(
            state.invoice,
            state.recommendation,
            is_duplicate=state.is_duplicate,
            thresholds=self._thresholds,
            correlation_id=state.correlation_id,
        )
        return {"decision": decision}
