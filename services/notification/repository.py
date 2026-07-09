"""State behind a Protocol - Notification's idempotency guard only (M10).

Unlike ApprovalRepository/PaymentRepository, there is no models.py here - no
domain record to define beyond the tracking_id key itself. The store's only
job is "have we already sent a notification for this tracking_id?" (before)
and "record that we just did" (after) - presence in the store IS the state.
Stores only delivery markers - never persists the notification's content;
a future "what exactly did we send" audit trail would need a different
mechanism, not an extension of this repository.

No list/enumerate method - unlike Approval's list_pending()/Payment's
list_all(), nothing in this project ever needs to enumerate all notified
tracking_ids (no GET /notifications listing endpoint is required by any
doc). Only ever point lookups and point writes.

InMemoryNotificationRepository is the only implementation used in tests.
DaprStateNotificationRepository (dapr_state_repository.py) is the real one.
"""

from __future__ import annotations

from typing import Protocol


class NotificationRepository(Protocol):
    async def already_notified(self, tracking_id: str) -> bool: ...
    async def mark_notified(self, tracking_id: str) -> None: ...


class InMemoryNotificationRepository:
    def __init__(self) -> None:
        self._notified: set[str] = set()

    async def already_notified(self, tracking_id: str) -> bool:
        return tracking_id in self._notified

    async def mark_notified(self, tracking_id: str) -> None:
        self._notified.add(tracking_id)
