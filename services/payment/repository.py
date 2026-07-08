"""State behind Protocols - same pattern as ApprovalRepository, applied
twice: PaymentRecord and Budget are two genuinely distinct pieces of state
Payment owns, not one repository wearing two hats.

InMemoryPaymentRepository/InMemoryBudgetRepository are the only
implementations today; used in tests. DaprStatePaymentRepository/
DaprStateBudgetRepository (dapr_state_repository.py) are the real ones.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Protocol

from services.payment.models import Budget, PaymentRecord


class PaymentRepository(Protocol):
    async def save(self, payment: PaymentRecord) -> None: ...
    async def get(self, tracking_id: str) -> PaymentRecord | None: ...
    async def list_all(self) -> list[PaymentRecord]: ...


class InMemoryPaymentRepository:
    """tracking_id is the sole lookup key (see PaymentRecord's docstring).
    list_all() returns items in insertion order."""

    def __init__(self) -> None:
        self._by_tracking_id: dict[str, PaymentRecord] = {}

    async def save(self, payment: PaymentRecord) -> None:
        self._by_tracking_id[payment.tracking_id] = payment

    async def get(self, tracking_id: str) -> PaymentRecord | None:
        return self._by_tracking_id.get(tracking_id)

    async def list_all(self) -> list[PaymentRecord]:
        return list(self._by_tracking_id.values())


class BudgetNotFoundError(Exception):
    """Raised when reserve()/release()/get() is asked about a department
    that was never seeded via ensure_seeded() - a configuration gap, not a
    concurrency failure."""


class InsufficientBudgetError(Exception):
    """Raised by reserve() when the department's remaining balance can't
    cover the requested amount, even after retrying past any transient
    optimistic-concurrency conflicts (Dapr-backed implementation only) - a
    terminal business outcome (INV-1014's loser), not a plumbing failure."""


class BudgetCorruptionError(Exception):
    """Raised by release() when crediting the requested amount back would
    push remaining above total - remaining starts equal to total and only
    ever moves in matched reserve/release pairs, so exceeding total is proof
    of a double-release or other corruption, never a legitimate business
    state. Not implementation-specific - both InMemoryBudgetRepository and
    DaprStateBudgetRepository raise this same type."""


class BudgetRepository(Protocol):
    async def ensure_seeded(self, department: str, total: Decimal) -> None: ...
    async def get(self, department: str) -> Budget | None: ...
    async def reserve(self, department: str, amount: Decimal) -> None: ...
    async def release(self, department: str, amount: Decimal) -> None: ...


class InMemoryBudgetRepository:
    """asyncio.Lock-guarded - proves the saga's reserve/release branching
    logic correctly under real intra-process concurrency (asyncio.gather),
    but is not the mechanism that proves INV-1014 across two real,
    independent containers/processes racing a shared Redis key - that is
    DaprStateBudgetRepository's ETag loop, proven live over docker compose."""

    def __init__(self) -> None:
        self._budgets: dict[str, Budget] = {}
        self._lock = asyncio.Lock()

    async def ensure_seeded(self, department: str, total: Decimal) -> None:
        async with self._lock:
            if department not in self._budgets:
                self._budgets[department] = Budget(
                    department=department, total=total, remaining=total
                )

    async def get(self, department: str) -> Budget | None:
        return self._budgets.get(department)

    async def reserve(self, department: str, amount: Decimal) -> None:
        async with self._lock:
            budget = self._budgets.get(department)
            if budget is None:
                raise BudgetNotFoundError(department)
            if budget.remaining < amount:
                raise InsufficientBudgetError(department)
            self._budgets[department] = budget.model_copy(
                update={"remaining": budget.remaining - amount}
            )

    async def release(self, department: str, amount: Decimal) -> None:
        async with self._lock:
            budget = self._budgets.get(department)
            if budget is None:
                raise BudgetNotFoundError(department)
            new_remaining = budget.remaining + amount
            if new_remaining > budget.total:
                raise BudgetCorruptionError(
                    f"release() for {department} would push remaining "
                    f"({new_remaining}) above total ({budget.total}) - "
                    f"proof of a double-release/corruption, not a legitimate state."
                )
            self._budgets[department] = budget.model_copy(update={"remaining": new_remaining})
