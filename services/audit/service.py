"""Transport-agnostic Audit orchestration (F9) - a pure, observational
consumer of the choreography chain. Publishes nothing onward, never blocks
or influences approval/payment decisions, and never filters by route: F9
requires the trail to be complete, including reject/duplicate, so all three
handlers write unconditionally (contrast with Payment/Notification, which
filter to specific routes/resolutions).

No FastAPI/HTTP/Postgres knowledge here - same layering as
ApprovalService/PaymentService/NotificationService.
"""

from __future__ import annotations

import logging

from services.audit.models import AuditTrail
from services.audit.repository import AuditRepository
from shared.contracts.models import (
    ApprovalCompletedEvent,
    DecisionCompletedEvent,
    PaymentCompletedEvent,
)


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository
        self._logger = logging.getLogger(__name__)

    async def record_decision_completed(self, event: DecisionCompletedEvent) -> None:
        await self._repository.upsert_decision(event)
        self._logger.info(
            "audit_decision_recorded",
            extra={"correlation_id": event.decision.correlation_id},
        )

    async def record_approval_completed(self, event: ApprovalCompletedEvent) -> None:
        await self._repository.upsert_approval(event)
        self._logger.info(
            "audit_approval_recorded",
            extra={"correlation_id": event.decision.correlation_id},
        )

    async def record_payment_completed(self, event: PaymentCompletedEvent) -> None:
        await self._repository.upsert_payment(event)
        self._logger.info(
            "audit_payment_recorded",
            extra={"correlation_id": event.decision.correlation_id},
        )

    async def get_trail(self, tracking_id: str) -> AuditTrail | None:
        """Backs GET /audit/{tracking_id}."""
        return await self._repository.get(tracking_id)


def build_audit_service(repository: AuditRepository) -> AuditService:
    """Composition seam, same reason as build_notification_service/
    build_approval_service/build_payment_service."""
    return AuditService(repository)
