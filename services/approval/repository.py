"""State behind a Protocol - same pattern as InvoiceRepository.

InMemoryApprovalRepository is the only implementation today; used in tests.
DaprStateApprovalRepository (dapr_state_repository.py) is the real one.
"""

from __future__ import annotations

from typing import Protocol

from services.approval.models import PendingApproval


class ApprovalRepository(Protocol):
    async def save(self, approval: PendingApproval) -> None: ...
    async def get(self, tracking_id: str) -> PendingApproval | None: ...
    async def list_pending(self) -> list[PendingApproval]: ...


class InMemoryApprovalRepository:
    """tracking_id is the sole lookup key (see PendingApproval's docstring).

    list_pending() returns items in insertion order - the same "chronological
    escalation order, free from insertion order" property the Dapr-backed
    implementation provides via its append-only index.
    """

    def __init__(self) -> None:
        self._by_tracking_id: dict[str, PendingApproval] = {}

    async def save(self, approval: PendingApproval) -> None:
        self._by_tracking_id[approval.tracking_id] = approval

    async def get(self, tracking_id: str) -> PendingApproval | None:
        return self._by_tracking_id.get(tracking_id)

    async def list_pending(self) -> list[PendingApproval]:
        return list(self._by_tracking_id.values())
