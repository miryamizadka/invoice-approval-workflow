"""The real, only production NotificationChannel - there is no real
email/SMS/webhook backend in this project, so this IS production, not a
stub (same posture as Payment's SimulatedPaymentGateway).

Named "Logging", not "Simulated" (Payment's specific choice, reasoned about
for its own reasons: it manufactures a synthetic decline for configured
invoice ids). This channel does neither: it never manufactures a failure
for anything, and logging genuinely IS the delivery mechanism here, not a
stand-in for a not-yet-built one - so "Logging" describes what it actually,
permanently does, rather than implying a temporary/fake substitute.
"""

from __future__ import annotations

import logging

from shared.contracts.models import Invoice


class LoggingNotificationChannel:
    """Current production notification channel. Real email/SMS/webhook
    providers can implement the same NotificationChannel Protocol later
    without changing NotificationService at all."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("notification.channel")

    async def send(self, invoice: Invoice, tracking_id: str, message: str, *, source: str) -> None:
        # Message content is interpolated directly into the primary log
        # message (not solely via extra=) - JsonFormatter only promotes
        # `correlation_id` out of extra; everything else in extra is
        # silently dropped from the emitted JSON line (confirmed by reading
        # services/payment/logging_config.py). Verification depends on this
        # text being visible via `docker compose logs notification`.
        self._logger.info(
            "notification_delivered correlation_id=%s invoice_id=%s submitter=%s "
            "source=%s message=%s",
            tracking_id, invoice.id, invoice.submitter, source, message,
            extra={"correlation_id": tracking_id},
        )
