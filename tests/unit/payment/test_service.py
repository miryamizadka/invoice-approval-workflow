"""Unit tests for PaymentService - the Manager orchestrating the payment
saga (M9 Saga/Compensation, M10 Idempotency).

Uses InMemoryPaymentRepository/InMemoryBudgetRepository, a stub
PaymentOutcomePublisher, and FakePaymentGateway - no Dapr.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from services.payment.accessors.fake_gateway import FakePaymentGateway
from services.payment.models import PaymentRecord, PaymentStatus
from services.payment.repository import (
    BudgetNotFoundError,
    InMemoryBudgetRepository,
    InMemoryPaymentRepository,
)
from services.payment.service import (
    PaymentNotFoundError,
    PaymentService,
    build_payment_service,
)
from shared.contracts.models import (
    ApprovalResolution,
    Decision,
    DecisionCompletedEvent,
    PaymentCompletedEvent,
    PaymentResolution,
    Route,
)
from tests.support.decision_fixtures import clean_invoice
from tests.support.event_fixtures import approval_completed_event, decision_completed_event

_DEPARTMENT = "engineering-2026Q2"
_BUDGET_TOTAL = Decimal("50000.00")


class _StubOutcomePublisher:
    def __init__(self) -> None:
        self.published: list[PaymentCompletedEvent] = []

    async def publish(self, event: PaymentCompletedEvent) -> None:
        self.published.append(event)


def _decision(route: Route = Route.AUTO_APPROVE, correlation_id: str = "corr-1") -> Decision:
    return Decision(
        route=route, reason="test reason", triggered_rules=[], correlation_id=correlation_id
    )


async def _service(
    **overrides: Any,
) -> tuple[
    PaymentService,
    InMemoryPaymentRepository,
    InMemoryBudgetRepository,
    _StubOutcomePublisher,
    FakePaymentGateway,
]:
    repository = overrides.get("repository") or InMemoryPaymentRepository()
    budget_repository = overrides.get("budget_repository") or InMemoryBudgetRepository()
    publisher = overrides.get("publisher") or _StubOutcomePublisher()
    gateway = overrides.get("gateway") or FakePaymentGateway()
    if not overrides.get("skip_seed"):
        await budget_repository.ensure_seeded(_DEPARTMENT, _BUDGET_TOTAL)
    service = build_payment_service(repository, budget_repository, publisher, gateway)
    return service, repository, budget_repository, publisher, gateway


# --- route/resolution filtering -------------------------------------------


@pytest.mark.parametrize("route", [Route.HUMAN_REVIEW, Route.REJECT, Route.DUPLICATE])
async def test_handle_decision_completed_ignores_non_auto_approve_routes(route: Route) -> None:
    """Explicitly covers Route.DUPLICATE (F3): a duplicate invoice must never
    reach payment, now that Intake publishes decision.completed directly for
    known duplicates (previously this route only reached Payment via a
    synthetic test event, never a real one)."""
    service, repository, _, _, gateway = await _service()

    await service.handle_decision_completed(decision_completed_event(route=route))

    assert await repository.list_all() == []
    assert gateway.charged == []


async def test_handle_approval_completed_ignores_rejected_resolution() -> None:
    service, repository, _, _, gateway = await _service()

    await service.handle_approval_completed(approval_completed_event(ApprovalResolution.REJECTED))

    assert await repository.list_all() == []
    assert gateway.charged == []


# --- happy path, both entrypoints converge --------------------------------


async def test_handle_decision_completed_reserves_and_completes_on_success() -> None:
    service, repository, budget_repository, publisher, gateway = await _service()

    await service.handle_decision_completed(decision_completed_event())

    record = await repository.get("corr-1")
    assert record is not None
    assert record.status == PaymentStatus.COMPLETED
    assert record.reserved_amount == Decimal("50.00")
    budget = await budget_repository.get(_DEPARTMENT)
    assert budget is not None
    assert budget.remaining == _BUDGET_TOTAL - Decimal("50.00")
    assert len(publisher.published) == 1
    assert publisher.published[0].resolution == PaymentResolution.COMPLETED
    assert gateway.charged == ["TEST-0000"]


async def test_handle_approval_completed_reserves_and_completes_on_success() -> None:
    service, repository, _, publisher, _ = await _service()

    await service.handle_approval_completed(approval_completed_event())

    record = await repository.get("corr-1")
    assert record is not None
    assert record.status == PaymentStatus.COMPLETED
    assert len(publisher.published) == 1


async def test_both_entrypoints_converge_on_identical_outcome_shape() -> None:
    service_a, repository_a, _, _, _ = await _service()
    service_b, repository_b, _, _, _ = await _service()

    await service_a.handle_decision_completed(decision_completed_event())
    await service_b.handle_approval_completed(approval_completed_event())

    record_a = await repository_a.get("corr-1")
    record_b = await repository_b.get("corr-1")
    assert record_a is not None and record_b is not None
    assert record_a.status == record_b.status == PaymentStatus.COMPLETED


# --- gateway failure -> compensation --------------------------------------


async def test_gateway_failure_triggers_compensation_and_releases_reserved_amount() -> None:
    gateway = FakePaymentGateway(fail_for={"TEST-0000"})
    service, repository, budget_repository, publisher, _ = await _service(gateway=gateway)

    await service.handle_decision_completed(decision_completed_event())

    record = await repository.get("corr-1")
    assert record is not None
    assert record.status == PaymentStatus.FAILED
    budget = await budget_repository.get(_DEPARTMENT)
    assert budget is not None
    assert budget.remaining == _BUDGET_TOTAL  # released back to baseline
    assert len(publisher.published) == 1
    assert publisher.published[0].resolution == PaymentResolution.FAILED


async def test_compensation_releases_exact_reserved_amount_not_recomputed_from_invoice() -> None:
    """reserved_amount is stored on the record and released verbatim - never
    re-derived from invoice.total at release time."""
    gateway = FakePaymentGateway(fail_for={"TEST-0000"})
    budget_repository = InMemoryBudgetRepository()
    await budget_repository.ensure_seeded(_DEPARTMENT, _BUDGET_TOTAL)
    service, repository, _, _, _ = await _service(
        gateway=gateway, budget_repository=budget_repository
    )

    await service.handle_decision_completed(decision_completed_event())

    budget = await budget_repository.get(_DEPARTMENT)
    assert budget is not None
    assert budget.remaining == _BUDGET_TOTAL


# --- insufficient budget / unconfigured department ------------------------


async def test_insufficient_budget_rejects_immediately_without_calling_gateway() -> None:
    budget_repository = InMemoryBudgetRepository()
    await budget_repository.ensure_seeded(_DEPARTMENT, Decimal("10.00"))  # less than invoice total
    service, repository, _, publisher, gateway = await _service(
        budget_repository=budget_repository, skip_seed=True
    )

    await service.handle_decision_completed(decision_completed_event())

    record = await repository.get("corr-1")
    assert record is not None
    assert record.status == PaymentStatus.FAILED
    assert "insufficient" in (record.reason or "").lower()
    assert gateway.charged == []
    assert len(publisher.published) == 1


async def test_insufficient_budget_performs_no_compensation() -> None:
    budget_repository = InMemoryBudgetRepository()
    await budget_repository.ensure_seeded(_DEPARTMENT, Decimal("10.00"))
    service, _, _, _, _ = await _service(budget_repository=budget_repository, skip_seed=True)

    await service.handle_decision_completed(decision_completed_event())

    budget = await budget_repository.get(_DEPARTMENT)
    assert budget is not None
    assert budget.remaining == Decimal("10.00")  # untouched - nothing was ever reserved


async def test_missing_budget_configuration_rejects_as_failed() -> None:
    service, repository, _, publisher, gateway = await _service(skip_seed=True)  # never seeded

    await service.handle_decision_completed(decision_completed_event())

    record = await repository.get("corr-1")
    assert record is not None
    assert record.status == PaymentStatus.FAILED
    assert "no budget configured" in (record.reason or "").lower()
    assert gateway.charged == []
    assert len(publisher.published) == 1


# --- idempotency: redelivery after terminal status ------------------------


async def test_redelivery_after_completed_status_is_a_noop() -> None:
    service, repository, _, publisher, gateway = await _service()
    event = decision_completed_event()
    await service.handle_decision_completed(event)

    await service.handle_decision_completed(event)
    await service.handle_decision_completed(event)

    assert len(gateway.charged) == 1
    assert len(publisher.published) == 1


async def test_redelivery_after_failed_status_is_a_noop() -> None:
    gateway = FakePaymentGateway(fail_for={"TEST-0000"})
    service, repository, _, publisher, _ = await _service(gateway=gateway)
    event = decision_completed_event()
    await service.handle_decision_completed(event)

    await service.handle_decision_completed(event)

    assert len(publisher.published) == 1


async def test_cross_topic_redelivery_for_the_same_tracking_id_converges_safely() -> None:
    service, repository, _, publisher, gateway = await _service()

    await service.handle_decision_completed(decision_completed_event())
    await service.handle_approval_completed(approval_completed_event())

    assert len(gateway.charged) == 1
    assert len(publisher.published) == 1


# --- crash recovery from RESERVED - two separate tests, success vs failure ---


async def test_crash_recovery_resumes_from_reserved_status_without_re_reserving() -> None:
    """MUST-HAVE - blocks Definition-of-Done. Simulates a crash between
    reserve() and the charge finishing: a PaymentRecord already exists at
    RESERVED before the handler ever runs. Redelivery must resume exactly
    at the charge step, never re-reserving."""
    repository = InMemoryPaymentRepository()
    budget_repository = InMemoryBudgetRepository()
    await budget_repository.ensure_seeded(_DEPARTMENT, _BUDGET_TOTAL)
    await budget_repository.reserve(_DEPARTMENT, Decimal("50.00"))  # pre-crash reserve
    await repository.save(
        PaymentRecord(
            tracking_id="corr-1",
            invoice=clean_invoice(total=Decimal("50.00")),
            decision=_decision(),
            status=PaymentStatus.RESERVED,
            department=_DEPARTMENT,
            reserved_amount=Decimal("50.00"),
        )
    )
    service, _, _, publisher, gateway = await _service(
        repository=repository, budget_repository=budget_repository, skip_seed=True
    )

    await service.handle_decision_completed(decision_completed_event())

    record = await repository.get("corr-1")
    assert record is not None
    assert record.status == PaymentStatus.COMPLETED
    assert gateway.charged == ["TEST-0000"]
    budget = await budget_repository.get(_DEPARTMENT)
    assert budget is not None
    assert budget.remaining == _BUDGET_TOTAL - Decimal("50.00")  # not double-reserved
    assert len(publisher.published) == 1


async def test_crash_recovery_resume_that_then_fails_still_compensates_correctly() -> None:
    """Distinct test from the success-path resume above (not a combined
    'combo' test) - the resume path can also lead to a gateway failure, and
    compensation must still release the correct reserved_amount."""
    repository = InMemoryPaymentRepository()
    budget_repository = InMemoryBudgetRepository()
    await budget_repository.ensure_seeded(_DEPARTMENT, _BUDGET_TOTAL)
    await budget_repository.reserve(_DEPARTMENT, Decimal("50.00"))
    await repository.save(
        PaymentRecord(
            tracking_id="corr-1",
            invoice=clean_invoice(total=Decimal("50.00")),
            decision=_decision(),
            status=PaymentStatus.RESERVED,
            department=_DEPARTMENT,
            reserved_amount=Decimal("50.00"),
        )
    )
    gateway = FakePaymentGateway(fail_for={"TEST-0000"})
    service, _, _, publisher, _ = await _service(
        repository=repository, budget_repository=budget_repository, gateway=gateway, skip_seed=True
    )

    await service.handle_decision_completed(decision_completed_event())

    record = await repository.get("corr-1")
    assert record is not None
    assert record.status == PaymentStatus.FAILED
    budget = await budget_repository.get(_DEPARTMENT)
    assert budget is not None
    assert budget.remaining == _BUDGET_TOTAL  # released back correctly
    assert len(publisher.published) == 1


# --- invariant guard -------------------------------------------------------


async def test_execute_charge_raises_runtime_error_for_non_reserved_status() -> None:
    repository = InMemoryPaymentRepository()
    record = PaymentRecord(
        tracking_id="corr-1",
        invoice=clean_invoice(),
        decision=_decision(),
        status=PaymentStatus.COMPLETED,  # not RESERVED - precondition violated
        department=_DEPARTMENT,
        reserved_amount=Decimal("50.00"),
    )
    service, _, _, _, _ = await _service(repository=repository)

    with pytest.raises(RuntimeError):
        await service._execute_charge(record)  # noqa: SLF001 - testing the internal guard directly


# --- read APIs -------------------------------------------------------------


async def test_list_all_returns_items_in_chronological_order() -> None:
    service, _, _, _, _ = await _service()
    await service.handle_decision_completed(decision_completed_event(route=Route.AUTO_APPROVE))
    second_event = DecisionCompletedEvent(
        invoice=clean_invoice(id="TEST-0002", total=Decimal("50.00")),
        decision=_decision(correlation_id="corr-2"),
    )
    await service.handle_decision_completed(second_event)

    result = await service.list_all()

    assert [r.tracking_id for r in result] == ["corr-1", "corr-2"]


async def test_get_returns_payment_record() -> None:
    service, _, _, _, _ = await _service()
    await service.handle_decision_completed(decision_completed_event())

    result = await service.get("corr-1")

    assert result.tracking_id == "corr-1"


async def test_get_raises_not_found_for_unknown_tracking_id() -> None:
    service, _, _, _, _ = await _service()

    with pytest.raises(PaymentNotFoundError):
        await service.get("missing")


async def test_get_budget_returns_seeded_budget() -> None:
    service, _, _, _, _ = await _service()

    budget = await service.get_budget(_DEPARTMENT)

    assert budget.department == _DEPARTMENT


async def test_get_budget_raises_not_found_for_unseeded_department() -> None:
    service, _, _, _, _ = await _service()

    with pytest.raises(BudgetNotFoundError):
        await service.get_budget("unknown-dept")
