"""Transport-agnostic Intake orchestration - no FastAPI/HTTP knowledge here.

Two-phase, matching Dapr pub/sub's fire-and-forget semantics: process()
publishes invoice.submitted and returns without waiting for a result;
complete() is called later - by the decision.completed subscription handler
in services/intake/app.py - once Decision's outcome event arrives. This is an
unavoidable structural consequence of moving off a synchronous HTTP call, not
an optional refactor: nothing here can synchronously return a Decision that
doesn't exist yet.
"""

from __future__ import annotations

import logging
import uuid

from services.intake.decision_completed_publisher import (
    DecisionCompletedPublisher,
    DecisionCompletedPublisherError,
)
from services.intake.decision_publisher import DecisionPublisher, DecisionPublisherError
from services.intake.models import Submission, SubmissionStatus
from services.intake.repository import InvoiceRepository
from shared.contracts.models import (
    Decision,
    DecisionCompletedEvent,
    Invoice,
    build_duplicate_decision,
    compute_dedup_key,
)


class IntakeService:
    def __init__(
        self,
        repository: InvoiceRepository,
        publisher: DecisionPublisher,
        decision_completed_publisher: DecisionCompletedPublisher,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._decision_completed_publisher = decision_completed_publisher
        self._logger = logging.getLogger(__name__)

    async def submit(self, invoice: Invoice) -> str:
        """Synchronous half: dedup check + store as received. Returns tracking_id immediately."""
        tracking_id = str(uuid.uuid4())
        dedup_key = compute_dedup_key(invoice)
        existing = await self._repository.find_by_dedup_key(dedup_key)
        submission = Submission(
            tracking_id=tracking_id,
            invoice=invoice,
            dedup_key=dedup_key,
            is_duplicate=existing is not None,
            status=SubmissionStatus.RECEIVED,
        )
        await self._repository.save(submission)
        self._logger.info(
            "invoice_received",
            extra={"correlation_id": tracking_id, "is_duplicate": submission.is_duplicate},
        )
        return tracking_id

    async def process(self, tracking_id: str) -> None:
        """Background half: publish invoice.submitted and return - does not
        wait for Decision's outcome (see complete()). A known duplicate never
        reaches publish() at all: running the agent for it would be pure
        waste (the router's gate 1 never even looks at the recommendation
        for a duplicate), so it's completed immediately with the same
        canonical DUPLICATE decision the router itself would produce - and,
        since Decision is never invoked for this case, decision.completed
        would otherwise never fire for it (Notification could never react to
        a duplicate outcome), so Intake publishes it directly instead."""
        submission = await self._repository.get(tracking_id)
        assert submission is not None  # scheduled right after save(); must exist
        if submission.is_duplicate:
            if submission.status == SubmissionStatus.COMPLETED:
                return  # idempotent no-op against a hypothetical repeat call
            decision = build_duplicate_decision(tracking_id)
            await self.complete(tracking_id, decision)
            try:
                await self._decision_completed_publisher.publish(
                    DecisionCompletedEvent(invoice=submission.invoice, decision=decision)
                )
            except DecisionCompletedPublisherError as exc:
                # The duplicate is already correctly COMPLETED above - this is
                # a side-channel notification failure, not a failure of the
                # duplicate detection itself, so the status is not reverted.
                # process() is a plain BackgroundTask (no Dapr redelivery), so
                # there is no retry for this specific publish today - accepted
                # non-goal, same category as other undelivered-side-effect
                # races already documented in this project.
                self._logger.error(
                    "failed to publish decision.completed for duplicate",
                    extra={
                        "correlation_id": tracking_id,
                        "route": decision.route.value,
                        "error": str(exc),
                    },
                )
            return
        # PROCESSING must be saved (awaited to completion) before publish() is
        # called: an inbound decision.completed event can arrive at any point
        # after publish() runs, and complete() must always find PROCESSING
        # already in place, never a stale RECEIVED. No concurrency between
        # these two awaits (same coroutine, sequential) - the ordering itself
        # closes the race window.
        await self._repository.save(
            submission.model_copy(update={"status": SubmissionStatus.PROCESSING})
        )
        try:
            await self._publisher.publish(submission.invoice, correlation_id=tracking_id)
        except DecisionPublisherError as exc:
            self._logger.error(
                "processing_failed", extra={"correlation_id": tracking_id, "error": str(exc)}
            )
            await self._repository.save(
                submission.model_copy(update={"status": SubmissionStatus.FAILED, "error": str(exc)})
            )

    async def complete(self, correlation_id: str, decision: Decision) -> None:
        """Called by the decision.completed subscription handler once
        Decision's outcome arrives. Triggered by an external, at-least-once
        event - a different trust boundary than process()'s internal call
        chain - so unlike process()'s `assert submission is not None`, this
        never crashes on a surprising input:
        - unknown tracking_id: logs a warning and returns. Dapr's retry
          wouldn't help (the submission will never materialize on its own) -
          most likely cause is InMemoryInvoiceRepository losing the
          submission on an Intake restart between publish() and this event
          arriving (see PLAN.md's documented gap).
        - already COMPLETED: idempotent no-op (normal at-least-once
          redelivery). A different decision for the same correlation_id on
          redelivery would be a real bug (not expected), so it's logged as a
          warning - but still first-write-wins, never overwritten.
        """
        submission = await self._repository.get(correlation_id)
        if submission is None:
            self._logger.warning(
                "received decision for unknown tracking_id",
                extra={"correlation_id": correlation_id},
            )
            return
        if submission.status == SubmissionStatus.COMPLETED:
            if submission.decision != decision:
                self._logger.warning(
                    "received a different decision for an already-completed tracking_id",
                    extra={"correlation_id": correlation_id},
                )
            return
        await self._repository.save(
            submission.model_copy(
                update={"status": SubmissionStatus.COMPLETED, "decision": decision}
            )
        )
        self._logger.info(
            "processing_completed",
            extra={"correlation_id": correlation_id, "route": decision.route.value},
        )

    async def get_status(self, tracking_id: str) -> Submission | None:
        return await self._repository.get(tracking_id)


def build_intake_service(
    repository: InvoiceRepository,
    publisher: DecisionPublisher,
    decision_completed_publisher: DecisionCompletedPublisher,
) -> IntakeService:
    """Composition seam, same reason as build_decider: keeps FastAPI out of this."""
    return IntakeService(repository, publisher, decision_completed_publisher)
