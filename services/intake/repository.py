"""State behind a Protocol - same pattern as LLMProvider.

InMemoryInvoiceRepository is the only implementation today; a future
PostgresInvoiceRepository can replace it without touching IntakeService.
"""

from __future__ import annotations

from typing import Protocol

from services.intake.models import Submission


class InvoiceRepository(Protocol):
    async def save(self, submission: Submission) -> None: ...
    async def get(self, tracking_id: str) -> Submission | None: ...
    async def find_by_dedup_key(self, dedup_key: str) -> Submission | None: ...


class InMemoryInvoiceRepository:
    """async def despite no real I/O - matches LLMProvider's reasoning: avoids
    a breaking signature change when a real-async Postgres implementation
    replaces this later.

    Concurrency note: find_by_dedup_key() then save() in IntakeService.submit()
    are two synchronous calls with no `await` between them, so they're race-free
    under a single event loop (uvicorn's default). A real multi-worker Postgres
    implementation would need an actual unique constraint on dedup_key, not just
    this ordering.
    """

    def __init__(self) -> None:
        self._by_tracking_id: dict[str, Submission] = {}
        self._by_dedup_key: dict[str, Submission] = {}

    async def save(self, submission: Submission) -> None:
        self._by_tracking_id[submission.tracking_id] = submission
        self._by_dedup_key[submission.dedup_key] = submission

    async def get(self, tracking_id: str) -> Submission | None:
        return self._by_tracking_id.get(tracking_id)

    async def find_by_dedup_key(self, dedup_key: str) -> Submission | None:
        return self._by_dedup_key.get(dedup_key)
