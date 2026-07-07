"""Per-rule boundary tests for the Decision router - independent of fixtures.

Fixture coverage only proves the chosen scenarios don't break; it says
nothing about whether a rule's threshold sits in the right place. Each test
below isolates exactly one rule at its exact boundary value, using a
policy-clean base invoice (see clean_invoice) so no other gate interferes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.decision.router import route_decision
from services.decision.router.config import DEFAULT_THRESHOLDS
from shared.contracts.models import (
    Category,
    Decision,
    Invoice,
    LineItem,
    Recommendation,
    RecommendationType,
    Route,
)
from tests.support.decision_fixtures import clean_invoice

_APPROVE_HIGH_CONFIDENCE = Recommendation(
    recommendation=RecommendationType.APPROVE, confidence=0.9, cited_rules=[], reasoning="clean"
)


def _decide(
    invoice: Invoice,
    recommendation: Recommendation | None = _APPROVE_HIGH_CONFIDENCE,
    *,
    is_duplicate: bool = False,
) -> Decision:
    return route_decision(
        invoice,
        recommendation,
        is_duplicate=is_duplicate,
        thresholds=DEFAULT_THRESHOLDS,
        correlation_id="cid",
    )


# --- AUTONOMY-CEILING ($250, inclusive) ---------------------------------


def test_ceiling_at_exactly_250_does_not_violate() -> None:
    # category=OTHER has no cap of its own, so only the ceiling gate is exercised.
    invoice = clean_invoice(category=Category.OTHER, attendees=None, total=Decimal("250.00"))
    decision = _decide(invoice)
    assert "AUTONOMY-CEILING" not in decision.triggered_rules
    assert decision.route == Route.AUTO_APPROVE


def test_ceiling_one_cent_over_violates() -> None:
    invoice = clean_invoice(category=Category.OTHER, attendees=None, total=Decimal("250.01"))
    decision = _decide(invoice)
    assert "AUTONOMY-CEILING" in decision.triggered_rules
    assert decision.route == Route.HUMAN_REVIEW


# --- AUTONOMY-CONFIDENCE (0.80, inclusive) ------------------------------


def test_confidence_at_exactly_0_80_does_not_violate() -> None:
    recommendation = Recommendation(
        recommendation=RecommendationType.APPROVE, confidence=0.80, cited_rules=[], reasoning="ok"
    )
    decision = _decide(clean_invoice(), recommendation)
    assert decision.route == Route.AUTO_APPROVE


def test_confidence_just_below_0_80_violates() -> None:
    recommendation = Recommendation(
        recommendation=RecommendationType.APPROVE, confidence=0.79, cited_rules=[], reasoning="ok"
    )
    decision = _decide(clean_invoice(), recommendation)
    assert decision.route == Route.HUMAN_REVIEW
    assert "AUTONOMY-CONFIDENCE" in decision.triggered_rules


# --- GLOBAL-RECEIPT ($25, exclusive - "over $25") -----------------------


def test_receipt_not_required_at_exactly_25_dollars() -> None:
    decision = _decide(clean_invoice(total=Decimal("25.00"), receipt_present=False))
    assert "GLOBAL-RECEIPT" not in decision.triggered_rules


def test_receipt_required_one_cent_over_25_dollars() -> None:
    decision = _decide(clean_invoice(total=Decimal("25.01"), receipt_present=False))
    assert "GLOBAL-RECEIPT" in decision.triggered_rules
    assert decision.route == Route.HUMAN_REVIEW


# --- GLOBAL-MATH (exact Decimal reconciliation) -------------------------


def test_math_reconciles_exactly() -> None:
    invoice = clean_invoice(
        line_items=[LineItem(description="x", quantity=Decimal("2"), unit_price=Decimal("10.00"))],
        tax_amount=Decimal("5.00"),
        total=Decimal("25.00"),
    )
    decision = _decide(invoice)
    assert "GLOBAL-MATH" not in decision.triggered_rules


def test_math_off_by_one_cent_violates() -> None:
    invoice = clean_invoice(
        line_items=[LineItem(description="x", quantity=Decimal("2"), unit_price=Decimal("10.00"))],
        tax_amount=Decimal("5.00"),
        total=Decimal("25.01"),
    )
    decision = _decide(invoice)
    assert "GLOBAL-MATH" in decision.triggered_rules
    assert decision.route == Route.HUMAN_REVIEW


# --- SAAS-01 ($200/mo, inclusive, under ceiling) ------------------------


def test_saas_cap_at_exactly_200_does_not_violate() -> None:
    invoice = clean_invoice(category=Category.SAAS, attendees=None, total=Decimal("200.00"))
    decision = _decide(invoice)
    assert "SAAS-01" not in decision.triggered_rules
    assert decision.route == Route.AUTO_APPROVE


def test_saas_cap_one_cent_over_violates() -> None:
    invoice = clean_invoice(category=Category.SAAS, attendees=None, total=Decimal("200.01"))
    decision = _decide(invoice)
    assert "SAAS-01" in decision.triggered_rules
    assert decision.route == Route.HUMAN_REVIEW


# --- HW-02 ($1,000, inclusive - but always above ceiling anyway) --------


def test_hardware_cap_at_exactly_1000_hw02_not_triggered() -> None:
    invoice = clean_invoice(category=Category.HARDWARE, attendees=None, total=Decimal("1000.00"))
    decision = _decide(invoice)
    assert "HW-02" not in decision.triggered_rules
    # Still escalates via AUTONOMY-CEILING ($1000 > $250) - that's correct, not a bug.
    assert "AUTONOMY-CEILING" in decision.triggered_rules


def test_hardware_cap_one_cent_over_1000_triggers_hw02() -> None:
    invoice = clean_invoice(category=Category.HARDWARE, attendees=None, total=Decimal("1000.01"))
    decision = _decide(invoice)
    assert "HW-02" in decision.triggered_rules


# --- MEAL-01 cap ($75/attendee, inclusive, under ceiling) ---------------


def test_meal_per_attendee_cap_at_exactly_75_does_not_violate() -> None:
    decision = _decide(clean_invoice(attendees=1, total=Decimal("75.00")))
    assert "MEAL-01" not in decision.triggered_rules
    assert decision.route == Route.AUTO_APPROVE


def test_meal_per_attendee_cap_one_cent_over_violates() -> None:
    decision = _decide(clean_invoice(attendees=1, total=Decimal("75.01")))
    assert "MEAL-01" in decision.triggered_rules
    assert decision.route == Route.HUMAN_REVIEW


# --- TRAVEL-02 ($1,500, exclusive - but always above ceiling anyway) ----


def test_travel_cap_at_exactly_1500_travel02_not_triggered() -> None:
    invoice = clean_invoice(category=Category.TRAVEL, attendees=None, total=Decimal("1500.00"))
    decision = _decide(invoice)
    assert "TRAVEL-02" not in decision.triggered_rules
    assert "AUTONOMY-CEILING" in decision.triggered_rules


def test_travel_cap_one_cent_over_1500_triggers_travel02() -> None:
    invoice = clean_invoice(category=Category.TRAVEL, attendees=None, total=Decimal("1500.01"))
    decision = _decide(invoice)
    assert "TRAVEL-02" in decision.triggered_rules


# --- GLOBAL-VENDOR (boolean, no false positive) -------------------------


def test_vendor_known_true_no_false_positive() -> None:
    decision = _decide(clean_invoice(vendor_known=True))
    assert "GLOBAL-VENDOR" not in decision.triggered_rules
    assert decision.route == Route.AUTO_APPROVE


def test_vendor_known_false_triggers_hard_stop() -> None:
    decision = _decide(clean_invoice(vendor_known=False))
    assert "GLOBAL-VENDOR" in decision.triggered_rules
    assert decision.route == Route.HUMAN_REVIEW


# --- GLOBAL-FX (any non-USD currency, not just EUR) ---------------------


@pytest.mark.parametrize("currency", ["EUR", "GBP"])
def test_fx_non_usd_currency_always_hard_stops(currency: str) -> None:
    decision = _decide(clean_invoice(currency=currency))
    assert "GLOBAL-FX" in decision.triggered_rules
    assert decision.route == Route.HUMAN_REVIEW


def test_fx_usd_does_not_hard_stop() -> None:
    decision = _decide(clean_invoice(currency="USD"))
    assert "GLOBAL-FX" not in decision.triggered_rules


# --- Reject gate: only configured severe rules bind REJECT; an --------
# --- unverified REJECT still can't reach auto_approve -------------------


def test_reject_gate_fires_for_configured_severe_rule() -> None:
    recommendation = Recommendation(
        recommendation=RecommendationType.REJECT,
        confidence=0.9,
        cited_rules=["MEAL-03"],
        reasoning="alcohol only",
    )
    decision = _decide(clean_invoice(), recommendation)
    assert decision.route == Route.REJECT


def test_reject_gate_does_not_fire_for_unconfigured_rule() -> None:
    """An agent REJECT that cites no severe rule must not fall through to
    auto_approve - the router doesn't trust the rule enough to bind REJECT,
    but it also can't ignore that the agent itself doesn't trust the item.
    It escalates instead, with a distinct rule_id for auditability.
    """
    recommendation = Recommendation(
        recommendation=RecommendationType.REJECT,
        confidence=0.9,
        cited_rules=["SOME-OTHER-RULE"],
        reasoning="agent thinks reject but cites nothing severe",
    )
    decision = _decide(clean_invoice(), recommendation)
    assert decision.route == Route.HUMAN_REVIEW
    assert "AGENT-REJECT-UNVERIFIED" in decision.triggered_rules


# --- Duplicate gate: false branch is a no-op -----------------------------


def test_duplicate_false_does_not_affect_clean_invoice() -> None:
    decision = _decide(clean_invoice(), is_duplicate=False)
    assert decision.route == Route.AUTO_APPROVE


# --- Combined boundary: ceiling and confidence both at the edge ----------


def test_combined_boundary_ceiling_and_confidence_both_at_edge_inclusive() -> None:
    recommendation = Recommendation(
        recommendation=RecommendationType.APPROVE, confidence=0.80, cited_rules=[], reasoning="ok"
    )
    invoice = clean_invoice(category=Category.OTHER, attendees=None, total=Decimal("250.00"))
    decision = _decide(invoice, recommendation)
    assert decision.route == Route.AUTO_APPROVE
