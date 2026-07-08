"""Deterministic decision router (M12 / ADR-002).

The agent only recommends; this module is the sole code path that can
return Route.AUTO_APPROVE. Every gate reads only structured Invoice
fields - never invoice.notes - so free-text prompt-steering cannot
influence the outcome (anti-cheese, D5).
"""

from __future__ import annotations

from decimal import Decimal

from services.decision.router.config import AutonomyThresholds
from shared.contracts.models import (
    Category,
    Decision,
    Invoice,
    Recommendation,
    RecommendationType,
    Route,
    build_duplicate_decision,
)


def _line_items_total(invoice: Invoice) -> Decimal:
    return sum(
        (item.quantity * item.unit_price for item in invoice.line_items),
        start=Decimal("0"),
    )


def _math_reconciles(invoice: Invoice) -> bool:
    return _line_items_total(invoice) + invoice.tax_amount == invoice.total


def _category_cap_violation(invoice: Invoice, thresholds: AutonomyThresholds) -> str | None:
    if invoice.category == Category.MEALS:
        if invoice.attendees is not None and invoice.attendees > 0:
            per_attendee = invoice.total / invoice.attendees
            if per_attendee > thresholds.meal_per_attendee_cap:
                return "MEAL-01"
        return None
    if invoice.category == Category.SAAS:
        return "SAAS-01" if invoice.total > thresholds.saas_monthly_cap else None
    if invoice.category == Category.HARDWARE:
        return "HW-02" if invoice.total > thresholds.hardware_cap else None
    if invoice.category == Category.TRAVEL:
        return "TRAVEL-02" if invoice.total > thresholds.travel_manager_approval_threshold else None
    return None


def route_decision(
    invoice: Invoice,
    recommendation: Recommendation | None,
    *,
    is_duplicate: bool,
    thresholds: AutonomyThresholds,
    correlation_id: str,
) -> Decision:
    # Gate 1: duplicate. build_duplicate_decision is the single canonical
    # source for this shape - Intake's pre-publish short-circuit (see
    # IntakeService.process()) uses the exact same function, so the two
    # can never drift apart.
    if is_duplicate:
        return build_duplicate_decision(correlation_id)

    # Gate 2: deterministic hard stops - all structural, never LLM-derived.
    hard_stops: list[str] = []
    if not invoice.vendor_known:
        hard_stops.append("GLOBAL-VENDOR")
    if invoice.currency != "USD":
        hard_stops.append("GLOBAL-FX")
    if invoice.total > thresholds.receipt_required_above and not invoice.receipt_present:
        hard_stops.append("GLOBAL-RECEIPT")
    if invoice.category == Category.MEALS and invoice.attendees is None:
        hard_stops.append("MEAL-01")
    if not _math_reconciles(invoice):
        hard_stops.append("GLOBAL-MATH")
    if hard_stops:
        return Decision(
            route=Route.HUMAN_REVIEW,
            reason=f"Hard stop(s) triggered: {', '.join(hard_stops)}.",
            triggered_rules=hard_stops,
            correlation_id=correlation_id,
        )

    # Gate 3: agent recommends reject. Agent-driven (semantic judgment), but
    # the router only binds a REJECT outcome for rule_ids the config
    # explicitly treats as severe - it never blindly trusts an arbitrary
    # REJECT. Critically, a REJECT recommendation always terminates here
    # one way or the other: if the agent doesn't trust this item enough to
    # approve it, the router must not let it fall through to gates 4-6 and
    # end up at auto_approve on a technicality. An unverified reject (no
    # severe rule cited) escalates to a human instead.
    if recommendation is not None and recommendation.recommendation == RecommendationType.REJECT:
        severe = [
            rule for rule in recommendation.cited_rules if rule in thresholds.severe_violation_rules
        ]
        if severe:
            return Decision(
                route=Route.REJECT,
                reason=f"Severe policy violation: {', '.join(severe)}.",
                triggered_rules=severe,
                correlation_id=correlation_id,
            )
        return Decision(
            route=Route.HUMAN_REVIEW,
            reason="Agent recommended reject but cited no configured severe-violation rule.",
            triggered_rules=["AGENT-REJECT-UNVERIFIED"],
            correlation_id=correlation_id,
        )

    # Gate 4: ceiling + category cap.
    violations: list[str] = []
    if invoice.total > thresholds.ceiling:
        violations.append("AUTONOMY-CEILING")
    cap_violation = _category_cap_violation(invoice, thresholds)
    if cap_violation:
        violations.append(cap_violation)
    if violations:
        return Decision(
            route=Route.HUMAN_REVIEW,
            reason=f"Amount/category policy violation(s): {', '.join(violations)}.",
            triggered_rules=violations,
            correlation_id=correlation_id,
        )

    # Gate 5: agent signal.
    if recommendation is None:
        return Decision(
            route=Route.HUMAN_REVIEW,
            reason="No agent recommendation available.",
            triggered_rules=[],
            correlation_id=correlation_id,
        )
    if recommendation.recommendation == RecommendationType.ESCALATE:
        return Decision(
            route=Route.HUMAN_REVIEW,
            reason="Agent recommended escalation.",
            triggered_rules=list(recommendation.cited_rules),
            correlation_id=correlation_id,
        )
    if recommendation.confidence < thresholds.min_confidence:
        return Decision(
            route=Route.HUMAN_REVIEW,
            reason=(
                f"Agent confidence {recommendation.confidence:.2f} is below the "
                f"{thresholds.min_confidence:.2f} threshold."
            ),
            triggered_rules=["AUTONOMY-CONFIDENCE"],
            correlation_id=correlation_id,
        )

    # Gate 6: every gate passed.
    return Decision(
        route=Route.AUTO_APPROVE,
        reason="Within autonomy ceiling and category policy, known vendor, receipt present, "
        "high agent confidence.",
        triggered_rules=[],
        correlation_id=correlation_id,
    )
