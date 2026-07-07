"""LangGraph state for the Decision agent."""

from __future__ import annotations

from pydantic import BaseModel

from services.decision.models import Decision, Invoice, Recommendation


class AgentState(BaseModel):
    """State passed between the preprocess -> classify -> router nodes.

    `prepared_prompt` is an internal communication artifact between
    preprocess and classify, not domain data - nodes can only communicate
    through the state, so it has to live here, but it is not something an
    auditor or caller should read as business information.

    `raw_llm_response` is kept for audit (F9): the exact text the LLM
    returned, even on a successful parse, so a decision can be traced back
    to precisely what the model said - not just the parsed Recommendation.
    """

    invoice: Invoice
    policy: str
    correlation_id: str
    is_duplicate: bool = False
    prepared_prompt: str | None = None
    raw_llm_response: str | None = None
    recommendation: Recommendation | None = None
    decision: Decision | None = None
