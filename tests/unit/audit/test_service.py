"""Unit tests for AuditService - the pure-consumer projection builder (F9).

Uses InMemoryAuditRepository - no Postgres. Verifies the "monotonic
enrichment" invariant (each event only fills its own columns, never
overwrites what an earlier event already wrote) and order-independence
(any of the three events can create the row first).
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from services.audit.repository import InMemoryAuditRepository
from services.audit.service import AuditService, build_audit_service
from shared.contracts.models import ApprovalResolution, PaymentResolution, Route
from tests.support.decision_fixtures import clean_invoice
from tests.support.event_fixtures import (
    approval_completed_event,
    decision_completed_event,
    payment_completed_event,
)


def _service() -> tuple[AuditService, InMemoryAuditRepository]:
    repository = InMemoryAuditRepository()
    return build_audit_service(repository), repository


# --- decision.completed creates the row -------------------------------------


async def test_decision_completed_creates_row_with_route_and_rules() -> None:
    service, repository = _service()

    await service.record_decision_completed(decision_completed_event(Route.AUTO_APPROVE))

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.route == Route.AUTO_APPROVE
    assert trail.tracking_id == "corr-1"


async def test_decision_completed_stores_recommendation_even_for_auto_approve() -> None:
    """The exact gap this service closes: today, recommendation is only ever
    persisted for human_review items (Approval's PendingApproval) - for
    auto_approve/reject/duplicate, it is lost. Audit must store it for
    every route recommendation is present for."""
    from shared.contracts.models import Recommendation, RecommendationType

    recommendation = Recommendation(
        recommendation=RecommendationType.APPROVE,
        confidence=0.87,
        cited_rules=["R1"],
        reasoning="looks fine",
    )
    service, repository = _service()

    await service.record_decision_completed(
        decision_completed_event(Route.AUTO_APPROVE, recommendation=recommendation)
    )

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.recommendation_type == RecommendationType.APPROVE
    assert trail.recommendation_confidence == 0.87
    assert trail.recommendation_reasoning == "looks fine"


async def test_decision_completed_with_no_recommendation_leaves_it_null() -> None:
    """recommendation is None only when the agent itself failed (AgentError
    fallback) - the row must still be created, just without those columns."""
    service, repository = _service()

    await service.record_decision_completed(
        decision_completed_event(Route.HUMAN_REVIEW, recommendation=None)
    )

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.recommendation_type is None


# --- monotonic enrichment: later events add columns, never overwrite -------


async def test_approval_completed_enriches_existing_row_without_overwriting_decision_fields() -> (
    None
):
    # Route.REJECT deliberately, not HUMAN_REVIEW (approval_completed_event's
    # own hardcoded default) - if the approval handler ever overwrote route
    # with its own event's Decision instead of leaving it alone, a matching
    # default would hide the bug. Passing `decision=` explicitly is the real
    # fix; the mismatched route is a belt-and-suspenders regression guard.
    service, repository = _service()
    decision_event = decision_completed_event(Route.REJECT)
    await service.record_decision_completed(decision_event)

    await service.record_approval_completed(
        approval_completed_event(ApprovalResolution.APPROVED, decision=decision_event.decision)
    )

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.route == Route.REJECT  # untouched by the approval handler
    assert trail.approval_resolution == ApprovalResolution.APPROVED


async def test_payment_completed_enriches_existing_row() -> None:
    service, repository = _service()
    decision_event = decision_completed_event(Route.REJECT)
    await service.record_decision_completed(decision_event)
    await service.record_approval_completed(
        approval_completed_event(ApprovalResolution.APPROVED, decision=decision_event.decision)
    )

    await service.record_payment_completed(
        payment_completed_event(PaymentResolution.COMPLETED, decision=decision_event.decision)
    )

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.route == Route.REJECT
    assert trail.approval_resolution == ApprovalResolution.APPROVED
    assert trail.payment_resolution == PaymentResolution.COMPLETED


# --- order independence: approval/payment can arrive before decision -------


async def test_approval_completed_can_create_the_row_if_decision_completed_not_seen_yet() -> None:
    """Guards against a real risk raised in review: a Dapr redelivery race
    where approval.completed/payment.completed arrives before decision.completed.
    Both carry a full invoice+decision copy, so the row must still be created
    correctly, just without recommendation_* / decision_completed_at."""
    service, repository = _service()

    await service.record_approval_completed(approval_completed_event(ApprovalResolution.APPROVED))

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.route == Route.HUMAN_REVIEW
    assert trail.recommendation_type is None
    assert trail.approval_resolution == ApprovalResolution.APPROVED


async def test_payment_completed_can_create_the_row_if_nothing_seen_yet() -> None:
    service, repository = _service()

    await service.record_payment_completed(payment_completed_event(PaymentResolution.COMPLETED))

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.payment_resolution == PaymentResolution.COMPLETED


async def test_decision_completed_arriving_after_approval_fills_in_recommendation() -> None:
    from shared.contracts.models import Recommendation, RecommendationType

    recommendation = Recommendation(
        recommendation=RecommendationType.ESCALATE,
        confidence=0.4,
        cited_rules=["R2"],
        reasoning="needs a human",
    )
    service, repository = _service()
    await service.record_approval_completed(approval_completed_event(ApprovalResolution.REJECTED))

    await service.record_decision_completed(
        decision_completed_event(Route.HUMAN_REVIEW, recommendation=recommendation)
    )

    trail = await repository.get("corr-1")
    assert trail is not None
    assert trail.approval_resolution == ApprovalResolution.REJECTED  # not overwritten
    assert trail.recommendation_type == RecommendationType.ESCALATE


# --- read API ----------------------------------------------------------------


async def test_get_trail_returns_none_for_unknown_tracking_id() -> None:
    service, _ = _service()

    assert await service.get_trail("does-not-exist") is None


# --- logging: correlation_id on every write (M14 convention) ---------------


async def test_record_decision_completed_logs_with_correlation_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service, _ = _service()

    with caplog.at_level(logging.INFO, logger="services.audit.service"):
        await service.record_decision_completed(decision_completed_event(Route.AUTO_APPROVE))

    record = next(r for r in caplog.records if r.getMessage().startswith("audit_decision_recorded"))
    assert record.correlation_id == "corr-1"  # type: ignore[attr-defined]


# --- get_summary() - F8 Dashboard aggregation --------------------------------


async def test_get_summary_with_no_data_returns_zero_rates_and_empty_dicts() -> None:
    service, _ = _service()

    summary = await service.get_summary()

    assert summary.total_invoices == 0
    assert summary.auto_approval_rate == 0.0
    assert summary.human_escalation_rate == 0.0
    assert summary.money_auto_approved == {}
    assert summary.money_human_approved == {}
    assert summary.counts_by_route == {}


async def test_get_summary_computes_auto_approval_rate_and_money() -> None:
    service, _ = _service()
    await service.record_decision_completed(
        decision_completed_event(
            Route.AUTO_APPROVE, correlation_id="a1", invoice=clean_invoice(total=Decimal("100.00"))
        )
    )
    await service.record_decision_completed(
        decision_completed_event(Route.HUMAN_REVIEW, correlation_id="a2")
    )

    summary = await service.get_summary()

    assert summary.total_invoices == 2
    assert summary.auto_approved_count == 1
    assert summary.auto_approval_rate == 0.5
    assert summary.money_auto_approved == {"USD": Decimal("100.00")}


async def test_get_summary_human_review_approved_counts_as_money_human_approved() -> None:
    service, _ = _service()
    decision_event = decision_completed_event(
        Route.HUMAN_REVIEW, correlation_id="h1", invoice=clean_invoice(total=Decimal("200.00"))
    )
    await service.record_decision_completed(decision_event)
    await service.record_approval_completed(
        approval_completed_event(
            ApprovalResolution.APPROVED,
            correlation_id="h1",
            invoice=decision_event.invoice,
            decision=decision_event.decision,
        )
    )

    summary = await service.get_summary()

    assert summary.human_review_count == 1
    assert summary.human_escalation_rate == 1.0
    assert summary.money_human_approved == {"USD": Decimal("200.00")}


async def test_get_summary_human_review_rejected_counts_toward_rate_but_not_money() -> None:
    """The critical distinction (escalated != approved): a human_review
    invoice that was REJECTED still counts toward human_escalation_rate (it
    WAS escalated) but must NOT appear in money_human_approved (the human
    did not approve it)."""
    service, _ = _service()
    decision_event = decision_completed_event(
        Route.HUMAN_REVIEW, correlation_id="h1", invoice=clean_invoice(total=Decimal("300.00"))
    )
    await service.record_decision_completed(decision_event)
    await service.record_approval_completed(
        approval_completed_event(
            ApprovalResolution.REJECTED,
            correlation_id="h1",
            invoice=decision_event.invoice,
            decision=decision_event.decision,
        )
    )

    summary = await service.get_summary()

    assert summary.human_review_count == 1
    assert summary.human_escalation_rate == 1.0
    assert summary.money_human_approved == {}


async def test_get_summary_handles_multiple_currencies_separately() -> None:
    service, _ = _service()
    await service.record_decision_completed(
        decision_completed_event(
            Route.AUTO_APPROVE,
            correlation_id="u1",
            invoice=clean_invoice(currency="USD", total=Decimal("100.00")),
        )
    )
    await service.record_decision_completed(
        decision_completed_event(
            Route.AUTO_APPROVE,
            correlation_id="e1",
            invoice=clean_invoice(currency="EUR", total=Decimal("50.00")),
        )
    )

    summary = await service.get_summary()

    assert summary.money_auto_approved == {"USD": Decimal("100.00"), "EUR": Decimal("50.00")}


async def test_get_summary_reject_and_duplicate_count_toward_total_not_rate_numerators() -> None:
    service, _ = _service()
    await service.record_decision_completed(
        decision_completed_event(Route.REJECT, correlation_id="r1")
    )
    await service.record_decision_completed(
        decision_completed_event(Route.DUPLICATE, correlation_id="d1")
    )
    await service.record_decision_completed(
        decision_completed_event(Route.AUTO_APPROVE, correlation_id="a1")
    )

    summary = await service.get_summary()

    assert summary.total_invoices == 3
    assert summary.auto_approved_count == 1
    assert summary.human_review_count == 0
    assert summary.auto_approval_rate == pytest.approx(1 / 3)
    assert summary.counts_by_route == {"reject": 1, "duplicate": 1, "auto_approve": 1}


async def test_get_summary_mixed_currencies_and_resolutions_combined() -> None:
    """The most dangerous case: currency grouping and approval_resolution
    filtering must not bleed into each other."""
    service, _ = _service()
    usd_approved = decision_completed_event(
        Route.HUMAN_REVIEW,
        correlation_id="h-usd-approved",
        invoice=clean_invoice(currency="USD", total=Decimal("100.00")),
    )
    await service.record_decision_completed(usd_approved)
    await service.record_approval_completed(
        approval_completed_event(
            ApprovalResolution.APPROVED,
            correlation_id="h-usd-approved",
            invoice=usd_approved.invoice,
            decision=usd_approved.decision,
        )
    )
    eur_approved = decision_completed_event(
        Route.HUMAN_REVIEW,
        correlation_id="h-eur-approved",
        invoice=clean_invoice(currency="EUR", total=Decimal("60.00")),
    )
    await service.record_decision_completed(eur_approved)
    await service.record_approval_completed(
        approval_completed_event(
            ApprovalResolution.APPROVED,
            correlation_id="h-eur-approved",
            invoice=eur_approved.invoice,
            decision=eur_approved.decision,
        )
    )
    # Same currency (USD) as the approved one, but rejected - must not leak
    # into money_human_approved's USD bucket.
    usd_rejected = decision_completed_event(
        Route.HUMAN_REVIEW,
        correlation_id="h-usd-rejected",
        invoice=clean_invoice(currency="USD", total=Decimal("999.00")),
    )
    await service.record_decision_completed(usd_rejected)
    await service.record_approval_completed(
        approval_completed_event(
            ApprovalResolution.REJECTED,
            correlation_id="h-usd-rejected",
            invoice=usd_rejected.invoice,
            decision=usd_rejected.decision,
        )
    )

    summary = await service.get_summary()

    assert summary.human_review_count == 3
    assert summary.money_human_approved == {"USD": Decimal("100.00"), "EUR": Decimal("60.00")}


async def test_get_summary_includes_generated_at_timestamp() -> None:
    service, _ = _service()

    summary = await service.get_summary()

    assert summary.generated_at is not None
