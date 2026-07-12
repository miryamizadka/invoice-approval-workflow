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
from datetime import UTC, datetime
from decimal import Decimal

from services.audit.models import AuditTrail, DashboardSummary
from services.audit.repository import AuditRepository
from shared.contracts.models import (
    ApprovalCompletedEvent,
    ApprovalResolution,
    DecisionCompletedEvent,
    PaymentCompletedEvent,
    Route,
)


def _add_amount(bucket: dict[str, Decimal], currency: str, amount: Decimal) -> None:
    bucket[currency] = bucket.get(currency, Decimal("0")) + amount


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

    async def get_summary(self) -> DashboardSummary:
        """Backs GET /audit/summary (F8) - computes the four dashboard
        metrics from the repository's raw route/currency/resolution buckets.
        This is the business-logic half (deciding what "auto approval rate"
        etc. mean); the repository only groups and counts."""
        buckets = await self._repository.get_route_summary()
        total = sum(b.count for b in buckets)
        auto_count = sum(b.count for b in buckets if b.route == Route.AUTO_APPROVE)
        human_count = sum(b.count for b in buckets if b.route == Route.HUMAN_REVIEW)
        money_auto: dict[str, Decimal] = {}
        money_human_approved: dict[str, Decimal] = {}
        counts_by_route: dict[str, int] = {}
        for b in buckets:
            counts_by_route[b.route.value] = counts_by_route.get(b.route.value, 0) + b.count
            if b.route == Route.AUTO_APPROVE:
                _add_amount(money_auto, b.currency, b.total)
            approved = ApprovalResolution.APPROVED
            if b.route == Route.HUMAN_REVIEW and b.approval_resolution == approved:
                _add_amount(money_human_approved, b.currency, b.total)
        return DashboardSummary(
            generated_at=datetime.now(UTC),
            total_invoices=total,
            auto_approved_count=auto_count,
            human_review_count=human_count,
            auto_approval_rate=(auto_count / total) if total else 0.0,
            human_escalation_rate=(human_count / total) if total else 0.0,
            money_auto_approved=money_auto,
            money_human_approved=money_human_approved,
            counts_by_route=counts_by_route,
        )


def build_audit_service(repository: AuditRepository) -> AuditService:
    """Composition seam, same reason as build_notification_service/
    build_approval_service/build_payment_service."""
    return AuditService(repository)
