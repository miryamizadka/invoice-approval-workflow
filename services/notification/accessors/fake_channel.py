"""Test double for NotificationChannel - never wired through production
composition, only ever constructed directly by tests."""

from __future__ import annotations

from services.notification.accessors.notification_channel import NotificationChannelError
from shared.contracts.models import Invoice


class FakeNotificationChannel:
    def __init__(
        self, *, fail_for: set[str] | None = None, fail_first_n_calls: int = 0
    ) -> None:
        self._fail_for = fail_for or set()
        self._fail_first_n_calls = fail_first_n_calls
        self._call_count = 0
        # (invoice.id, tracking_id, message, source)
        self.sent: list[tuple[str, str, str, str]] = []

    async def send(self, invoice: Invoice, tracking_id: str, message: str, *, source: str) -> None:
        self._call_count += 1
        if invoice.id in self._fail_for or self._call_count <= self._fail_first_n_calls:
            raise NotificationChannelError("simulated channel failure")
        self.sent.append((invoice.id, tracking_id, message, source))
