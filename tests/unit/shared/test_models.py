"""Tests for shared cross-service contracts that don't belong to one service.

build_duplicate_decision: the single canonical "what does a DUPLICATE
decision look like" source, used by both the router's gate 1 and Intake's
pre-publish short-circuit (see IntakeService.process()) - must never drift.

InvoiceSubmittedEvent: the pub/sub envelope Intake publishes and Decision
subscribes to - carries transport concerns (correlation_id) that Invoice
itself deliberately doesn't.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.contracts.models import (
    ApprovalCompletedEvent,
    ApprovalResolution,
    Decision,
    DecisionCompletedEvent,
    DecisionOutcome,
    InvoiceSubmittedEvent,
    Recommendation,
    RecommendationType,
    Route,
    build_duplicate_decision,
)
from tests.support.decision_fixtures import clean_invoice


def _decision(route: Route = Route.HUMAN_REVIEW, correlation_id: str = "corr-1") -> Decision:
    return Decision(route=route, reason="test", triggered_rules=[], correlation_id=correlation_id)


def _recommendation() -> Recommendation:
    return Recommendation(
        recommendation=RecommendationType.ESCALATE,
        confidence=0.5,
        cited_rules=["AUTONOMY-CEILING"],
        reasoning="over ceiling",
    )


def test_build_duplicate_decision_returns_duplicate_route() -> None:
    decision = build_duplicate_decision("corr-123")

    assert decision.route == Route.DUPLICATE
    assert decision.triggered_rules == ["GLOBAL-DUP"]
    assert decision.correlation_id == "corr-123"
    assert "duplicate" in decision.reason.lower()


def test_build_duplicate_decision_is_a_decision() -> None:
    assert isinstance(build_duplicate_decision("corr-123"), Decision)


def test_invoice_submitted_event_carries_invoice_and_correlation_id() -> None:
    invoice = clean_invoice()

    event = InvoiceSubmittedEvent(invoice=invoice, correlation_id="corr-456")

    assert event.invoice == invoice
    assert event.correlation_id == "corr-456"


def test_invoice_submitted_event_is_frozen() -> None:
    event = InvoiceSubmittedEvent(invoice=clean_invoice(), correlation_id="corr-456")

    with pytest.raises(ValidationError):
        event.correlation_id = "other"  # type: ignore[misc]


def test_invoice_submitted_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        InvoiceSubmittedEvent(
            invoice=clean_invoice(), correlation_id="corr-456", is_duplicate=False  # type: ignore[call-arg]
        )


def test_decision_outcome_carries_decision_and_recommendation() -> None:
    decision = _decision()
    recommendation = _recommendation()

    outcome = DecisionOutcome(decision=decision, recommendation=recommendation)

    assert outcome.decision == decision
    assert outcome.recommendation == recommendation


def test_decision_outcome_recommendation_defaults_to_none() -> None:
    outcome = DecisionOutcome(decision=_decision())

    assert outcome.recommendation is None


def test_decision_completed_event_carries_invoice_decision_and_recommendation() -> None:
    invoice = clean_invoice()
    decision = _decision()
    recommendation = _recommendation()

    event = DecisionCompletedEvent(
        invoice=invoice, decision=decision, recommendation=recommendation
    )

    assert event.invoice == invoice
    assert event.decision == decision
    assert event.recommendation == recommendation


def test_decision_completed_event_recommendation_defaults_to_none() -> None:
    event = DecisionCompletedEvent(invoice=clean_invoice(), decision=_decision())

    assert event.recommendation is None


def test_decision_completed_event_is_frozen() -> None:
    event = DecisionCompletedEvent(invoice=clean_invoice(), decision=_decision())

    with pytest.raises(ValidationError):
        event.decision = _decision(route=Route.AUTO_APPROVE)  # type: ignore[misc]


def test_approval_completed_event_carries_invoice_decision_and_resolution() -> None:
    invoice = clean_invoice()
    decision = _decision()

    event = ApprovalCompletedEvent(
        invoice=invoice, decision=decision, resolution=ApprovalResolution.APPROVED
    )

    assert event.invoice == invoice
    assert event.decision == decision
    assert event.resolution == ApprovalResolution.APPROVED


def test_approval_completed_event_is_frozen() -> None:
    event = ApprovalCompletedEvent(
        invoice=clean_invoice(), decision=_decision(), resolution=ApprovalResolution.REJECTED
    )

    with pytest.raises(ValidationError):
        event.resolution = ApprovalResolution.APPROVED  # type: ignore[misc]
