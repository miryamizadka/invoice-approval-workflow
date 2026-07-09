"""Transport-agnostic Notification orchestration - the terminal, pure
consumer of the choreography chain (publishes nothing onward). No FastAPI/
HTTP knowledge here (same layering as ApprovalService/PaymentService).

Idempotency (M10): before sending, checks NotificationRepository; if a mark
already exists for this tracking_id, this is a no-op (Dapr redelivery-safe).
send() is called BEFORE mark_notified() - if channel.send() raises, the mark
is never written, so a Dapr redelivery correctly retries the actual send
(the only failure mode is a harmless duplicate log line if the process
crashes between a successful send() and the mark persisting - never a
silently-skipped notification). channel.send()'s exception is never caught
here; it propagates uncaught out of the subscription handler (Dapr sees a
non-2xx, redelivers later) - the same "infra failure not swallowed" posture
as BudgetRepositoryError/PaymentRepositoryError in Payment.

Accepted non-goal (same category as Payment's reserve/save race): the three
steps of _notify() (already_notified -> send -> mark_notified) are not
atomic together. Two near-simultaneous redeliveries of the same event could
both observe already_notified=False before either sends - a duplicate
notification (not dangerous, just unpleasant). Low-risk in practice, not
fixed here.

The three routes/resolutions filtered here are mutually exclusive by
construction (Route/ApprovalResolution/PaymentResolution never overlap for
the same tracking_id - e.g. a REJECT-routed invoice never also produces an
approval.completed or payment.completed event) - so at most one of the three
handlers below ever fires for a given tracking_id; the idempotency guard is
defense against redelivery of the *same* event, not against overlap between
different event types.
"""

from __future__ import annotations

import logging

from services.notification.accessors.notification_channel import NotificationChannel
from services.notification.repository import NotificationRepository
from shared.contracts.models import (
    ApprovalCompletedEvent,
    ApprovalResolution,
    Decision,
    DecisionCompletedEvent,
    Invoice,
    PaymentCompletedEvent,
    Route,
)

_TERMINAL_DECISION_ROUTES = {Route.REJECT, Route.DUPLICATE}


def _reject_or_duplicate_message(decision: Decision) -> str:
    if decision.route == Route.DUPLICATE:
        return f"Invoice identified as duplicate: {decision.reason}"
    return f"Invoice rejected: {decision.reason}"


def _approval_rejected_message(decision: Decision) -> str:
    # TODO (documented on purpose, not "mysterious"): decision.reason here is
    # the *original escalation reason* (why it went to human review), not the
    # approver's own rationale for rejecting - ApprovalService.reject() does
    # not collect a separate rejection reason. This is the best text
    # available today, not a perfect fit - closing this would need a new
    # reason parameter on reject(), out of scope for this phase.
    return f"Rejected by approver: {decision.reason}"


def _payment_message(event: PaymentCompletedEvent) -> str:
    # Deliberately not rewritten to a hardcoded "Payment completed
    # successfully."/"Payment failed: ..." - event.reason is already phrased
    # as a complete statement in PaymentService's own code ("payment executed
    # successfully" on success, the gateway's decline message on failure).
    # Rewriting it here would create two sources of truth for the same text.
    return f"Payment {event.resolution.value}: {event.reason}"


class NotificationService:
    def __init__(self, repository: NotificationRepository, channel: NotificationChannel) -> None:
        self._repository = repository
        self._channel = channel
        self._logger = logging.getLogger(__name__)

    async def handle_decision_completed(self, event: DecisionCompletedEvent) -> None:
        """Only REJECT/DUPLICATE act here - closes a previously-undocumented
        gap: these two terminal outcomes had no push path at all
        (only pull via GET /invoices/{id}). AUTO_APPROVE/HUMAN_REVIEW are
        ignored - they get their eventual notification via payment.completed
        / approval.completed respectively."""
        if event.decision.route not in _TERMINAL_DECISION_ROUTES:
            return
        message = _reject_or_duplicate_message(event.decision)
        tracking_id = event.decision.correlation_id
        await self._notify(event.invoice, tracking_id, message, source="decision.completed")

    async def handle_approval_completed(self, event: ApprovalCompletedEvent) -> None:
        """Only REJECTED acts here - APPROVED gets its eventual notification
        via payment.completed once the saga finishes."""
        if event.resolution != ApprovalResolution.REJECTED:
            return
        message = _approval_rejected_message(event.decision)
        tracking_id = event.decision.correlation_id
        await self._notify(event.invoice, tracking_id, message, source="approval.completed")

    async def handle_payment_completed(self, event: PaymentCompletedEvent) -> None:
        """Both COMPLETED and FAILED act here unconditionally - no filter
        needed (ARCHITECTURE.md's flowchart: both DONE and FAILED paths lead
        to the same NOTIFY node)."""
        message = _payment_message(event)
        tracking_id = event.decision.correlation_id
        await self._notify(event.invoice, tracking_id, message, source="payment.completed")

    async def _notify(
        self, invoice: Invoice, tracking_id: str, message: str, *, source: str
    ) -> None:
        if await self._repository.already_notified(tracking_id):
            self._logger.info(
                "notification_already_sent_skipping", extra={"correlation_id": tracking_id}
            )
            return
        await self._channel.send(invoice, tracking_id, message, source=source)
        await self._repository.mark_notified(tracking_id)
        self._logger.info("notification_sent", extra={"correlation_id": tracking_id})

    async def already_notified(self, tracking_id: str) -> bool:
        """Backs the debug GET /notifications/{tracking_id} endpoint."""
        return await self._repository.already_notified(tracking_id)


def build_notification_service(
    repository: NotificationRepository, channel: NotificationChannel
) -> NotificationService:
    """Composition seam, same reason as build_approval_service/build_payment_service."""
    return NotificationService(repository, channel)
