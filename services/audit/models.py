"""Audit's own domain model: one row per tracking_id, monotonically
enriched as decision.completed / approval.completed / payment.completed
arrive (F9). This is a read-model/projection, not a source of truth - each
business service (Intake/Decision/Approval/Payment) keeps owning its own
operational state; AuditTrail exists only to make the cross-service history
queryable in one place.

Not a literal immutable append-only ledger: there is one row per
tracking_id, not one row per event. Each handler writes only the columns
its own event carries and never touches a column another handler already
populated (see AuditService/AuditRepository) - "monotonic enrichment", not
event sourcing.

recommendation_* / decision_completed_at are the only columns unique to
decision.completed - left None until that event is seen, so a row created
first by approval.completed or payment.completed (a Dapr redelivery/ordering
race) is still valid, just with those columns still empty.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from shared.contracts.models import (
    ApprovalResolution,
    Invoice,
    PaymentResolution,
    RecommendationType,
    Route,
)


class AuditTrail(BaseModel):
    tracking_id: str
    invoice_number: str
    vendor: str
    department: str
    category: str
    total: Decimal
    currency: str
    invoice: Invoice

    route: Route
    decision_reason: str
    triggered_rules: list[str]

    recommendation_type: RecommendationType | None = None
    recommendation_confidence: float | None = None
    recommendation_cited_rules: list[str] | None = None
    recommendation_reasoning: str | None = None
    decision_completed_at: datetime | None = None

    approval_resolution: ApprovalResolution | None = None
    approval_completed_at: datetime | None = None

    payment_resolution: PaymentResolution | None = None
    payment_reason: str | None = None
    payment_completed_at: datetime | None = None


class DashboardSummary(BaseModel):
    """GET /audit/summary's response (F8). All fields always present with a
    well-defined value - empty dict (not null) when there's no data yet,
    0.0 (not null) for rates - so the UI only ever needs an empty-state
    check, never a null-check.

    money_human_approved is specifically route=human_review AND
    approval_resolution=approved - NOT "any human-touched money" and NOT
    tied to payment success (that's Payment/M9's concern, a different
    metric). A human_review invoice that was rejected still counts toward
    human_review_count/human_escalation_rate (it WAS escalated) but never
    appears here.

    Money fields are per-currency (currency -> amount), never summed across
    currencies - invoices in this system are not all the same currency.
    """

    generated_at: datetime
    total_invoices: int = 0
    auto_approved_count: int = 0
    human_review_count: int = 0
    auto_approval_rate: float = 0.0
    human_escalation_rate: float = 0.0
    money_auto_approved: dict[str, Decimal] = {}
    money_human_approved: dict[str, Decimal] = {}
    counts_by_route: dict[str, int] = {}
