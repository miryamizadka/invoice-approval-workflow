"""Transport-agnostic Approval orchestration - no FastAPI/HTTP knowledge here
(same layering as IntakeService/Decider).

State machine (see PendingApproval's docstring for the diagram):
    PENDING       -> WAITING_INFO   (request_info)
    PENDING       -> APPROVED       (approve)
    PENDING       -> REJECTED       (reject)
    WAITING_INFO  -> APPROVED       (approve)
    WAITING_INFO  -> REJECTED       (reject)
    APPROVED/REJECTED -> (terminal - any further action raises
                          ApprovalAlreadyResolvedError)

Approval never computes a Recommendation itself - it only displays what
Decision already produced (recommendation may be None; see
DecisionCompletedEvent's docstring for when).
"""

from __future__ import annotations

import logging

from services.approval.models import ApprovalStatus, PendingApproval
from services.approval.outcome_publisher import ApprovalOutcomePublisher
from services.approval.repository import ApprovalRepository
from shared.contracts.models import (
    ApprovalCompletedEvent,
    ApprovalResolution,
    DecisionCompletedEvent,
    Route,
)

_ACTIONABLE_STATUSES = {ApprovalStatus.PENDING, ApprovalStatus.WAITING_INFO}


class ApprovalNotFoundError(Exception):
    """Raised when a tracking_id has no PendingApproval record - maps to 404."""


class ApprovalAlreadyResolvedError(Exception):
    """Raised on any action attempted against an APPROVED/REJECTED item -
    maps to 409. This is also the idempotency guard against double-click/retry:
    it prevents a duplicate approval.completed publish (e.g. a double payment
    downstream), not just a "nice" error message."""


class ApprovalService:
    def __init__(self, repository: ApprovalRepository, publisher: ApprovalOutcomePublisher) -> None:
        self._repository = repository
        self._publisher = publisher
        self._logger = logging.getLogger(__name__)

    async def handle_decision_completed(self, event: DecisionCompletedEvent) -> None:
        """Called by the decision.completed subscription handler. Ignores
        every route except HUMAN_REVIEW - choreography, Approval doesn't
        know or care who else is listening.

        Idempotency (redelivery fix): if a PendingApproval already exists for
        this tracking_id (in ANY status), this is a no-op - Dapr's at-least-
        once redelivery of decision.completed arriving after a human already
        acted must never revert the status back to PENDING. This method only
        ever creates a new record on first sighting; it never updates an
        existing one (all status changes after that happen exclusively via
        approve/reject/request_info)."""
        if event.decision.route != Route.HUMAN_REVIEW:
            return
        tracking_id = event.decision.correlation_id
        existing = await self._repository.get(tracking_id)
        if existing is not None:
            self._logger.warning(
                "received decision.completed for an already-tracked approval",
                extra={"correlation_id": tracking_id},
            )
            return
        await self._repository.save(
            PendingApproval(
                tracking_id=tracking_id,
                invoice=event.invoice,
                decision=event.decision,
                recommendation=event.recommendation,
                status=ApprovalStatus.PENDING,
            )
        )
        self._logger.info("escalated_for_review", extra={"correlation_id": tracking_id})

    async def list_pending(self) -> list[PendingApproval]:
        return await self._repository.list_pending()

    async def get(self, tracking_id: str) -> PendingApproval:
        approval = await self._repository.get(tracking_id)
        if approval is None:
            raise ApprovalNotFoundError(tracking_id)
        return approval

    async def approve(self, tracking_id: str) -> PendingApproval:
        return await self._resolve(
            tracking_id, ApprovalStatus.APPROVED, ApprovalResolution.APPROVED
        )

    async def reject(self, tracking_id: str) -> PendingApproval:
        return await self._resolve(
            tracking_id, ApprovalStatus.REJECTED, ApprovalResolution.REJECTED
        )

    async def _resolve(
        self, tracking_id: str, status: ApprovalStatus, resolution: ApprovalResolution
    ) -> PendingApproval:
        approval = await self._require_actionable(tracking_id)
        updated = approval.model_copy(update={"status": status})
        await self._repository.save(updated)
        await self._publisher.publish(
            ApprovalCompletedEvent(
                invoice=updated.invoice, decision=updated.decision, resolution=resolution
            )
        )
        self._logger.info(
            "approval_resolved",
            extra={"correlation_id": tracking_id, "resolution": resolution.value},
        )
        return updated

    async def request_info(self, tracking_id: str) -> PendingApproval:
        """Sends the item back to WAITING_INFO - no event published (see
        module docstring / ADR-003: once escalated, the human owns the
        decision). WAITING_INFO is not terminal: approve/reject remain valid
        from here."""
        approval = await self._require_actionable(tracking_id)
        updated = approval.model_copy(update={"status": ApprovalStatus.WAITING_INFO})
        await self._repository.save(updated)
        return updated

    async def _require_actionable(self, tracking_id: str) -> PendingApproval:
        approval = await self._repository.get(tracking_id)
        if approval is None:
            raise ApprovalNotFoundError(tracking_id)
        if approval.status not in _ACTIONABLE_STATUSES:
            raise ApprovalAlreadyResolvedError(tracking_id)
        return approval


def build_approval_service(
    repository: ApprovalRepository, publisher: ApprovalOutcomePublisher
) -> ApprovalService:
    """Composition seam, same reason as build_intake_service/build_decider."""
    return ApprovalService(repository, publisher)
