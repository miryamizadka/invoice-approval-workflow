"""NotificationChannel - the accessor NotificationService calls to actually
deliver a notification. A Protocol, same pattern as PaymentGateway/
LLMProvider: isolates NotificationService from any particular backend.

There is no real email/SMS/webhook backend in this project -
LoggingNotificationChannel (the real implementation) IS production, not a
stub, the same situation Payment is in with no real payment processor."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.contracts.models import Invoice


class NotificationChannelError(Exception):
    """Raised when the channel fails to deliver - never caught inside
    NotificationService; propagates uncaught so Dapr redelivers later."""


@runtime_checkable
class NotificationChannel(Protocol):
    async def send(self, invoice: Invoice, tracking_id: str, message: str, *, source: str) -> None:
        """Raises NotificationChannelError on failure. No return value -
        success is 'did not raise'. `source` is the triggering topic name
        (e.g. "payment.completed") - carried through purely for
        observability (log line context), not business logic."""
        ...
