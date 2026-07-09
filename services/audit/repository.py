"""State behind an AuditRepository Protocol - one row per tracking_id,
monotonically enriched by whichever of the three handlers has fired so far.

_base_fields() is shared by all three upsert_* methods because every one of
decision.completed/approval.completed/payment.completed carries a full
invoice+decision copy (shared/contracts/models.py) - so any of the three can
independently create the row (INSERT-shaped) if it is the first to arrive,
not just enrich an existing one. Only the event-specific fields differ per
method; base fields written by a later event are always identical to what an
earlier event already wrote (same invoice/decision objects), so re-writing
them on every call is idempotent, never a real overwrite.

InMemoryAuditRepository is the only implementation used in tests.
PostgresAuditRepository (postgres_repository.py) is the real one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from services.audit.models import AuditTrail
from shared.contracts.models import (
    ApprovalCompletedEvent,
    Decision,
    DecisionCompletedEvent,
    Invoice,
    PaymentCompletedEvent,
)


def _base_fields(invoice: Invoice, decision: Decision) -> dict[str, object]:
    return {
        "tracking_id": decision.correlation_id,
        "invoice_number": invoice.invoice_number,
        "vendor": invoice.vendor,
        "department": invoice.department,
        "category": invoice.category,
        "total": invoice.total,
        "currency": invoice.currency,
        "invoice": invoice,
        "route": decision.route,
        "decision_reason": decision.reason,
        "triggered_rules": decision.triggered_rules,
    }


class AuditRepository(Protocol):
    async def ensure_schema(self) -> None: ...
    async def upsert_decision(self, event: DecisionCompletedEvent) -> None: ...
    async def upsert_approval(self, event: ApprovalCompletedEvent) -> None: ...
    async def upsert_payment(self, event: PaymentCompletedEvent) -> None: ...
    async def get(self, tracking_id: str) -> AuditTrail | None: ...


class InMemoryAuditRepository:
    """tracking_id is the sole lookup key, same as every other repository in
    this project. ensure_schema() is a no-op here - a dict has no schema to
    create; it exists on the Protocol only so app.py's startup hook can call
    it unconditionally regardless of which implementation is wired in (same
    pattern as BudgetRepository.ensure_seeded)."""

    def __init__(self) -> None:
        self._by_tracking_id: dict[str, AuditTrail] = {}

    async def ensure_schema(self) -> None:
        return None

    async def upsert_decision(self, event: DecisionCompletedEvent) -> None:
        fields = _base_fields(event.invoice, event.decision)
        fields["decision_completed_at"] = datetime.now(UTC)
        if event.recommendation is not None:
            fields["recommendation_type"] = event.recommendation.recommendation
            fields["recommendation_confidence"] = event.recommendation.confidence
            fields["recommendation_cited_rules"] = event.recommendation.cited_rules
            fields["recommendation_reasoning"] = event.recommendation.reasoning
        self._merge(event.decision.correlation_id, fields)

    async def upsert_approval(self, event: ApprovalCompletedEvent) -> None:
        fields = _base_fields(event.invoice, event.decision)
        fields["approval_resolution"] = event.resolution
        fields["approval_completed_at"] = datetime.now(UTC)
        self._merge(event.decision.correlation_id, fields)

    async def upsert_payment(self, event: PaymentCompletedEvent) -> None:
        fields = _base_fields(event.invoice, event.decision)
        fields["payment_resolution"] = event.resolution
        fields["payment_reason"] = event.reason
        fields["payment_completed_at"] = datetime.now(UTC)
        self._merge(event.decision.correlation_id, fields)

    async def get(self, tracking_id: str) -> AuditTrail | None:
        return self._by_tracking_id.get(tracking_id)

    def _merge(self, tracking_id: str, fields: dict[str, object]) -> None:
        existing = self._by_tracking_id.get(tracking_id)
        self._by_tracking_id[tracking_id] = (
            AuditTrail(**fields) if existing is None else existing.model_copy(update=fields)
        )
