"""Unit tests for InMemoryPaymentRepository and InMemoryBudgetRepository."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from services.payment.models import Budget, PaymentRecord, PaymentStatus
from services.payment.repository import (
    BudgetCorruptionError,
    BudgetNotFoundError,
    InMemoryBudgetRepository,
    InMemoryPaymentRepository,
    InsufficientBudgetError,
)
from shared.contracts.models import Decision, Route
from tests.support.decision_fixtures import clean_invoice


def _decision() -> Decision:
    return Decision(
        route=Route.AUTO_APPROVE, reason="test reason", triggered_rules=[], correlation_id="corr-1"
    )


def _payment_record(**overrides: Any) -> PaymentRecord:
    base: dict[str, Any] = {
        "tracking_id": "tid-1",
        "invoice": clean_invoice(),
        "decision": _decision(),
        "status": PaymentStatus.RESERVED,
        "department": "engineering-2026Q2",
        "reserved_amount": Decimal("50.00"),
        "reason": None,
    }
    base.update(overrides)
    return PaymentRecord(**base)


# --- InMemoryPaymentRepository -------------------------------------------


@pytest.mark.asyncio
async def test_save_and_get_roundtrip() -> None:
    repository = InMemoryPaymentRepository()
    payment = _payment_record()

    await repository.save(payment)
    result = await repository.get("tid-1")

    assert result == payment


@pytest.mark.asyncio
async def test_get_returns_none_when_not_found() -> None:
    repository = InMemoryPaymentRepository()

    assert await repository.get("missing") is None


@pytest.mark.asyncio
async def test_list_all_returns_empty_when_none_saved() -> None:
    repository = InMemoryPaymentRepository()

    assert await repository.list_all() == []


@pytest.mark.asyncio
async def test_list_all_returns_items_in_insertion_order() -> None:
    repository = InMemoryPaymentRepository()
    first = _payment_record(tracking_id="tid-1")
    second = _payment_record(tracking_id="tid-2")

    await repository.save(first)
    await repository.save(second)

    assert await repository.list_all() == [first, second]


@pytest.mark.asyncio
async def test_save_updates_existing_record_without_duplicating_in_list() -> None:
    repository = InMemoryPaymentRepository()
    payment = _payment_record(tracking_id="tid-1")
    await repository.save(payment)

    updated = payment.model_copy(update={"status": PaymentStatus.COMPLETED})
    await repository.save(updated)

    assert await repository.get("tid-1") == updated
    assert await repository.list_all() == [updated]


# --- InMemoryBudgetRepository ---------------------------------------------


@pytest.mark.asyncio
async def test_ensure_seeded_creates_budget_with_full_remaining() -> None:
    repository = InMemoryBudgetRepository()

    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    budget = await repository.get("marketing-2026Q2")
    assert budget == Budget(
        department="marketing-2026Q2", total=Decimal("1000.00"), remaining=Decimal("1000.00")
    )


@pytest.mark.asyncio
async def test_ensure_seeded_is_a_noop_when_already_seeded() -> None:
    repository = InMemoryBudgetRepository()
    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    await repository.reserve("marketing-2026Q2", Decimal("400.00"))

    await repository.ensure_seeded("marketing-2026Q2", Decimal("9999.00"))

    budget = await repository.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("600.00")


@pytest.mark.asyncio
async def test_get_returns_none_for_unseeded_department() -> None:
    repository = InMemoryBudgetRepository()

    assert await repository.get("unknown-dept") is None


@pytest.mark.asyncio
async def test_reserve_deducts_from_remaining() -> None:
    repository = InMemoryBudgetRepository()
    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    await repository.reserve("marketing-2026Q2", Decimal("400.00"))

    budget = await repository.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("600.00")


@pytest.mark.asyncio
async def test_reserve_raises_insufficient_budget_error_when_amount_exceeds_remaining() -> None:
    repository = InMemoryBudgetRepository()
    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    with pytest.raises(InsufficientBudgetError):
        await repository.reserve("marketing-2026Q2", Decimal("1000.01"))


@pytest.mark.asyncio
async def test_reserve_raises_budget_not_found_error_for_unseeded_department() -> None:
    repository = InMemoryBudgetRepository()

    with pytest.raises(BudgetNotFoundError):
        await repository.reserve("unknown-dept", Decimal("1.00"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "amount, should_succeed",
    [(Decimal("1000.00"), True), (Decimal("1000.01"), False)],
)
async def test_reserve_boundary_exact_remaining(amount: Decimal, should_succeed: bool) -> None:
    repository = InMemoryBudgetRepository()
    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    if should_succeed:
        await repository.reserve("marketing-2026Q2", amount)
        budget = await repository.get("marketing-2026Q2")
        assert budget is not None
        assert budget.remaining == Decimal("0.00")
    else:
        with pytest.raises(InsufficientBudgetError):
            await repository.reserve("marketing-2026Q2", amount)


@pytest.mark.asyncio
async def test_release_adds_back_to_remaining() -> None:
    repository = InMemoryBudgetRepository()
    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))
    await repository.reserve("marketing-2026Q2", Decimal("400.00"))

    await repository.release("marketing-2026Q2", Decimal("400.00"))

    budget = await repository.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("1000.00")


@pytest.mark.asyncio
async def test_release_raises_budget_not_found_error_for_unseeded_department() -> None:
    repository = InMemoryBudgetRepository()

    with pytest.raises(BudgetNotFoundError):
        await repository.release("unknown-dept", Decimal("1.00"))


@pytest.mark.asyncio
async def test_release_raises_when_result_would_exceed_total() -> None:
    """Corruption guard: remaining must never legitimately exceed total -
    if it would, some caller released more than was ever reserved."""
    repository = InMemoryBudgetRepository()
    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    with pytest.raises(BudgetCorruptionError):
        await repository.release("marketing-2026Q2", Decimal("0.01"))


@pytest.mark.asyncio
async def test_concurrent_reserves_never_oversell() -> None:
    """Proves the branching logic under real intra-process concurrency - not
    a proof of the Redis/ETag mechanism across independent processes (that
    is DaprStateBudgetRepository's job, verified live over docker compose)."""
    repository = InMemoryBudgetRepository()
    await repository.ensure_seeded("marketing-2026Q2", Decimal("1000.00"))

    results = await asyncio.gather(
        repository.reserve("marketing-2026Q2", Decimal("600.00")),
        repository.reserve("marketing-2026Q2", Decimal("600.00")),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, InsufficientBudgetError)]
    successes = [r for r in results if r is None]
    assert len(failures) == 1
    assert len(successes) == 1
    budget = await repository.get("marketing-2026Q2")
    assert budget is not None
    assert budget.remaining == Decimal("400.00")
    assert budget.remaining >= Decimal("0.00")
