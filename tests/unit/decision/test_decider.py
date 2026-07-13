"""Tests for services/decision/service/decider.py's N5 (RAG) integration.

No test file existed for Decider before this phase - discovered as a real
gap during N5's design. Uses the real policy.md (load_policy_text()) and a
capturing stub provider that records the actual user_message sent to the
LLM, so these tests prove what the agent is actually shown, not just that
retrieve_relevant_policy_text() returns the right thing in isolation
(already covered by test_policy_index.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from services.decision.router.config import DEFAULT_THRESHOLDS
from services.decision.service.decider import build_decider
from services.decision.service.policy_loader import load_policy_text
from shared.contracts.models import Category, Recommendation, RecommendationType, Route
from tests.support.decision_fixtures import clean_invoice

REAL_POLICY = load_policy_text()


class _CapturingProvider:
    """Records the exact user_message it was called with, and returns a
    fixed, valid recommendation - a MockProvider that remembers its input."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.captured_user_messages: list[str] = []

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        json_mode: bool = False,
        schema: type[Any] | None = None,
    ) -> str:
        self.captured_user_messages.append(user_message)
        return self._response


VALID_RECOMMENDATION = Recommendation(
    recommendation=RecommendationType.APPROVE,
    confidence=0.9,
    cited_rules=[],
    reasoning="clean",
)


@pytest.mark.asyncio
async def test_decider_sends_only_the_retrieved_policy_to_the_llm() -> None:
    """The prompt actually sent to the LLM contains the invoice's own
    category section, but not an unrelated category's section - proving
    retrieval is wired into the real decide() path, not just available."""
    provider = _CapturingProvider(VALID_RECOMMENDATION.model_dump_json())
    decider = build_decider(provider, DEFAULT_THRESHOLDS, REAL_POLICY)
    invoice = clean_invoice(category=Category.MEALS)

    await decider.decide(invoice, correlation_id="cid-rag-1")

    assert len(provider.captured_user_messages) == 1
    sent_prompt = provider.captured_user_messages[0]
    assert "Meals & Entertainment" in sent_prompt
    assert "Hardware" not in sent_prompt
    assert "Travel" not in sent_prompt


@pytest.mark.asyncio
async def test_decider_falls_back_to_full_policy_when_retrieval_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-safe contract: retrieval is meant to improve relevance, never to
    be a new way for a decision to fail. Any retrieval exception must fall
    back to the full, original policy text - decide() must still succeed."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated retrieval failure")

    monkeypatch.setattr(
        "services.decision.service.decider.retrieve_relevant_policy_text", _raise
    )
    provider = _CapturingProvider(VALID_RECOMMENDATION.model_dump_json())
    decider = build_decider(provider, DEFAULT_THRESHOLDS, REAL_POLICY)
    invoice = clean_invoice(category=Category.MEALS)

    outcome = await decider.decide(invoice, correlation_id="cid-rag-2")

    assert outcome.decision.route == Route.AUTO_APPROVE
    sent_prompt = provider.captured_user_messages[0]
    assert REAL_POLICY in sent_prompt  # full policy, not a partial retrieval


@pytest.mark.asyncio
async def test_decider_full_decide_still_works_end_to_end_with_retrieval_enabled() -> None:
    """Regression guard: the full decide() flow (graph + router) is
    unaffected by routing through retrieval instead of the raw policy."""
    provider = _CapturingProvider(VALID_RECOMMENDATION.model_dump_json())
    decider = build_decider(provider, DEFAULT_THRESHOLDS, REAL_POLICY)
    invoice = clean_invoice()

    outcome = await decider.decide(invoice, correlation_id="cid-rag-3")

    assert outcome.decision.route == Route.AUTO_APPROVE
    assert outcome.recommendation == VALID_RECOMMENDATION
