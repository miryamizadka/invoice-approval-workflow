"""Shared builders for the choreography events (decision.completed,
approval.completed, payment.completed) consumed across Payment,
Notification, and Intake's tests.

Extracted after a real bug (Intake's decision.completed handler parsing a
bare Decision instead of the enriched DecisionCompletedEvent Decision
actually publishes) went undetected because each test file built its own
local, ad-hoc event payload. A single source for these shapes means any
future drift between what's tested and what production actually sends
fails every consumer's tests at once, not silently in just one of them.

Distinct from decision_fixtures.py, which builds Invoice/Recommendation for
the fixture-driven Decision router suite - a different concern from building
already-decided choreography events for downstream consumers.
"""

from __future__ import annotations

from shared.contracts.models import (
    ApprovalCompletedEvent,
    ApprovalResolution,
    Decision,
    DecisionCompletedEvent,
    Invoice,
    PaymentCompletedEvent,
    PaymentResolution,
    Recommendation,
    Route,
)
from tests.support.decision_fixtures import clean_invoice


def decision_completed_event(
    route: Route = Route.AUTO_APPROVE,
    *,
    correlation_id: str = "corr-1",
    invoice: Invoice | None = None,
    recommendation: Recommendation | None = None,
) -> DecisionCompletedEvent:
    decision = Decision(
        route=route, reason="test reason", triggered_rules=[], correlation_id=correlation_id
    )
    return DecisionCompletedEvent(
        invoice=invoice or clean_invoice(), decision=decision, recommendation=recommendation
    )


def approval_completed_event(
    resolution: ApprovalResolution = ApprovalResolution.APPROVED,
    *,
    correlation_id: str = "corr-1",
    invoice: Invoice | None = None,
    decision: Decision | None = None,
) -> ApprovalCompletedEvent:
    decision = decision or Decision(
        route=Route.HUMAN_REVIEW,
        reason="test reason",
        triggered_rules=[],
        correlation_id=correlation_id,
    )
    return ApprovalCompletedEvent(
        invoice=invoice or clean_invoice(), decision=decision, resolution=resolution
    )


def payment_completed_event(
    resolution: PaymentResolution,
    *,
    correlation_id: str = "corr-1",
    invoice: Invoice | None = None,
    reason: str = "test reason",
    decision: Decision | None = None,
) -> PaymentCompletedEvent:
    decision = decision or Decision(
        route=Route.AUTO_APPROVE,
        reason="test reason",
        triggered_rules=[],
        correlation_id=correlation_id,
    )
    return PaymentCompletedEvent(
        invoice=invoice or clean_invoice(), decision=decision, resolution=resolution, reason=reason
    )
