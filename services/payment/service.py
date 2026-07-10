"""Transport-agnostic Payment saga orchestration (ADR-004). Payment is the
sole coordinator: reserve budget -> execute payment, with a compensating
release on payment failure. No FastAPI/HTTP knowledge here.

Both subscription entrypoints below converge on _run_saga(), keyed solely by
tracking_id - so even a (never-expected) double trigger from both topics for
the same tracking_id converges safely through the same idempotency guard.

Business failures (InsufficientBudgetError, BudgetNotFoundError) are caught
here and become a terminal FAILED PaymentRecord. BudgetRepositoryError (a
genuine Dapr/infra failure, raised only after retries are exhausted) is
deliberately NOT caught here - it propagates uncaught out of the
subscription handler, causing a non-2xx response, so Dapr redelivers the
event later once the underlying infra issue clears. An infra hiccup must
never be silently written down as "this invoice's payment failed"."""

from __future__ import annotations

import logging
from decimal import Decimal

from services.payment.accessors.payment_gateway import PaymentGateway, PaymentGatewayError
from services.payment.models import Budget, PaymentRecord, PaymentStatus
from services.payment.outcome_publisher import PaymentOutcomePublisher
from services.payment.repository import (
    BudgetNotFoundError,
    BudgetRepository,
    InsufficientBudgetError,
    PaymentRepository,
)
from shared.contracts.models import (
    ApprovalCompletedEvent,
    ApprovalResolution,
    Decision,
    DecisionCompletedEvent,
    Invoice,
    PaymentCompletedEvent,
    PaymentResolution,
    Route,
)

_TERMINAL_STATUSES = {PaymentStatus.COMPLETED, PaymentStatus.FAILED}


class PaymentNotFoundError(Exception):
    """Raised when a tracking_id has no PaymentRecord - maps to 404."""


class PaymentService:
    def __init__(
        self,
        repository: PaymentRepository,
        budget_repository: BudgetRepository,
        publisher: PaymentOutcomePublisher,
        gateway: PaymentGateway,
    ) -> None:
        self._repository = repository
        self._budget_repository = budget_repository
        self._publisher = publisher
        self._gateway = gateway
        self._logger = logging.getLogger(__name__)

    async def handle_decision_completed(self, event: DecisionCompletedEvent) -> None:
        """Ignores every route except AUTO_APPROVE - choreography, same as
        Approval's filter on HUMAN_REVIEW."""
        if event.decision.route != Route.AUTO_APPROVE:
            return
        await self._run_saga(event.invoice, event.decision)

    async def handle_approval_completed(self, event: ApprovalCompletedEvent) -> None:
        """Ignores REJECTED - only an approved human decision triggers payment."""
        if event.resolution != ApprovalResolution.APPROVED:
            return
        await self._run_saga(event.invoice, event.decision)

    async def _run_saga(self, invoice: Invoice, decision: Decision) -> None:
        tracking_id = decision.correlation_id
        record = await self._repository.get(tracking_id)
        if record is not None and record.status in _TERMINAL_STATUSES:
            return  # idempotent no-op - redelivery after terminal, same as Approval
        if record is None:
            # Accepted race (accepted non-goal for Phase 6, see PLAN.md): two
            # near-simultaneous redeliveries of the same first-sighting event
            # can both observe record is None here before either saves -
            # a double reservation for one invoice. Not fixed here.
            try:
                await self._budget_repository.reserve(invoice.department, invoice.total)
            except InsufficientBudgetError:
                self._logger.warning(
                    "payment_rejected_insufficient_budget", extra={"correlation_id": tracking_id}
                )
                await self._reject(tracking_id, invoice, decision, "insufficient department budget")
                return
            except BudgetNotFoundError:
                self._logger.error(
                    "payment_rejected_unconfigured_department",
                    extra={"correlation_id": tracking_id, "department": invoice.department},
                )
                await self._reject(
                    tracking_id, invoice, decision,
                    f"no budget configured for department {invoice.department}",
                )
                return
            record = PaymentRecord(
                tracking_id=tracking_id,
                invoice=invoice,
                decision=decision,
                status=PaymentStatus.RESERVED,
                department=invoice.department,
                reserved_amount=invoice.total,
                reason=None,
            )
            await self._repository.save(record)
            self._logger.info("budget_reserved", extra={"correlation_id": tracking_id})
        # record.status == RESERVED here - either just reserved above, or
        # resumed after a crash between reserve() and the charge finishing.
        await self._execute_charge(record)

    async def _execute_charge(self, record: PaymentRecord) -> None:
        if record.status != PaymentStatus.RESERVED:
            raise RuntimeError(
                f"_execute_charge called with unexpected status {record.status!r} "
                f"for tracking_id {record.tracking_id!r} - _run_saga's control flow "
                f"guarantees RESERVED here; this is a programmer error, not a business outcome."
            )
        try:
            await self._gateway.charge(record.invoice, idempotency_key=record.tracking_id)
        except PaymentGatewayError as exc:
            await self._budget_repository.release(record.department, record.reserved_amount)
            updated = record.model_copy(update={"status": PaymentStatus.FAILED, "reason": str(exc)})
            await self._repository.save(updated)
            self._logger.warning(
                "payment_failed_compensated", extra={"correlation_id": record.tracking_id}
            )
            await self._publish(updated)
            return
        updated = record.model_copy(
            update={"status": PaymentStatus.COMPLETED, "reason": "payment executed successfully"}
        )
        await self._repository.save(updated)
        self._logger.info("payment_completed", extra={"correlation_id": record.tracking_id})
        await self._publish(updated)

    async def _reject(
        self, tracking_id: str, invoice: Invoice, decision: Decision, reason: str
    ) -> None:
        record = PaymentRecord(
            tracking_id=tracking_id,
            invoice=invoice,
            decision=decision,
            status=PaymentStatus.FAILED,
            department=invoice.department,
            reserved_amount=Decimal("0"),
            reason=reason,
        )
        await self._repository.save(record)
        await self._publish(record)

    async def _publish(self, record: PaymentRecord) -> None:
        resolution = (
            PaymentResolution.COMPLETED
            if record.status == PaymentStatus.COMPLETED
            else PaymentResolution.FAILED
        )
        await self._publisher.publish(
            PaymentCompletedEvent(
                invoice=record.invoice,
                decision=record.decision,
                resolution=resolution,
                reason=record.reason or "",
            )
        )
        self._logger.info("payment_event_published", extra={"correlation_id": record.tracking_id})

    async def list_all(self) -> list[PaymentRecord]:
        return await self._repository.list_all()

    async def get(self, tracking_id: str) -> PaymentRecord:
        record = await self._repository.get(tracking_id)
        if record is None:
            raise PaymentNotFoundError(tracking_id)
        return record

    async def get_budget(self, department: str) -> Budget:
        budget = await self._budget_repository.get(department)
        if budget is None:
            raise BudgetNotFoundError(department)
        return budget


def build_payment_service(
    repository: PaymentRepository,
    budget_repository: BudgetRepository,
    publisher: PaymentOutcomePublisher,
    gateway: PaymentGateway,
) -> PaymentService:
    """Composition seam, same reason as build_approval_service/build_decider."""
    return PaymentService(repository, budget_repository, publisher, gateway)
