"""Data contracts shared across services: Invoice, Recommendation, Decision.

These are the wire/domain shapes multiple independently-deployable services
(Decision, Intake, and future services) must agree on. Anything that is
specific to how one service decides or processes (e.g. the router's
AutonomyThresholds, the agent's state) stays owned by that service instead.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Category(StrEnum):
    MEALS = "meals"
    TRAVEL = "travel"
    SAAS = "saas"
    HARDWARE = "hardware"
    OTHER = "other"


class RecommendationType(StrEnum):
    APPROVE = "approve"
    ESCALATE = "escalate"
    REJECT = "reject"


class Route(StrEnum):
    AUTO_APPROVE = "auto_approve"
    HUMAN_REVIEW = "human_review"
    REJECT = "reject"
    DUPLICATE = "duplicate"


class LineItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    description: str
    quantity: Decimal
    unit_price: Decimal = Field(alias="unitPrice")


class Invoice(BaseModel):
    """Structured invoice data used across services - the domain shape,
    independent of any transport. See InvoiceSubmittedEvent for the
    envelope that carries this over the invoice.submitted pub/sub event."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="forbid")

    id: str
    submitter: str
    department: str
    vendor: str
    vendor_known: bool = Field(alias="vendorKnown")
    invoice_number: str = Field(alias="invoiceNumber")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    category: Category
    attendees: int | None = None
    line_items: list[LineItem] = Field(alias="lineItems")
    tax_amount: Decimal = Field(alias="taxAmount")
    total: Decimal
    receipt_present: bool = Field(alias="receiptPresent")
    date: date
    notes: str | None = None


class Recommendation(BaseModel):
    """Agent output. Advisory only — carries no routing authority (ADR-002 / M12)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recommendation: RecommendationType
    confidence: float = Field(ge=0.0, le=1.0)
    cited_rules: list[str]
    reasoning: str


class Decision(BaseModel):
    """Router output. The only object with authority to route a decision (M12)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: Route
    reason: str
    triggered_rules: list[str]
    correlation_id: str


class DecisionOutcome(BaseModel):
    """Decider's full internal result - Decision (router's binding output)
    plus the Recommendation that led to it. Internal to the Decision service
    only (Decider.decide()'s return type) - never crosses a service boundary
    itself. POST /decisions extracts just `.decision` (unchanged external API,
    D4); the invoice.submitted subscription handler uses both to build
    DecisionCompletedEvent, which is what actually crosses the boundary.
    recommendation is None only when the agent itself failed (AgentError
    fallback, which always routes to HUMAN_REVIEW)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Decision
    recommendation: Recommendation | None = None


class DecisionCompletedEvent(BaseModel):
    """decision.completed envelope (Dapr pub/sub) - invoice + decision +
    recommendation. Approval needs all three for F4 (display invoice data,
    the agent's recommendation, its confidence, and the policy reasons) -
    the bare Decision alone (route/reason/triggered_rules/correlation_id)
    doesn't carry the invoice or the agent's raw recommendation/confidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice: Invoice
    decision: Decision
    recommendation: Recommendation | None = None


class ApprovalResolution(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalCompletedEvent(BaseModel):
    """approval.completed envelope (Dapr pub/sub) - a single topic for both
    outcomes (mirrors decision.completed's reasoning: Approval doesn't know
    or care who's listening; a future Payment/Notification service filters
    by `resolution`). Carries the full Decision, not just tracking_id +
    resolution: Payment's idempotency keys are invoice-based (M10's
    pay:INV-1012 example), and Notification (reject path) plausibly needs
    decision.reason for the submitter-facing plain-language message (F2) -
    both future consumers are served by one shape, at near-zero cost since
    everything is already in memory at publish time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice: Invoice
    decision: Decision
    resolution: ApprovalResolution


class InvoiceSubmittedEvent(BaseModel):
    """Envelope Intake publishes and Decision subscribes to (invoice.submitted,
    Dapr pub/sub) - carries correlation_id, a transport concern Invoice itself
    doesn't. No is_duplicate field: Intake never publishes for an invoice it
    already knows is a duplicate (see IntakeService.process()'s short-circuit
    via build_duplicate_decision below) - reaching Decision through this event
    already proves the invoice is not a known duplicate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invoice: Invoice
    correlation_id: str


def build_duplicate_decision(correlation_id: str) -> Decision:
    """Canonical DUPLICATE decision shape (GLOBAL-DUP) - the single source
    both the router's gate 1 and Intake's pre-publish short-circuit use, so
    the two can never drift apart (same reasoning as compute_dedup_key below:
    one canonical definition, not a per-service copy)."""
    return Decision(
        route=Route.DUPLICATE,
        reason="Duplicate of an already-processed invoice (same vendor, invoice number, "
        "and total).",
        triggered_rules=["GLOBAL-DUP"],
        correlation_id=correlation_id,
    )


def compute_dedup_key(invoice: Invoice) -> str:
    """Pure dedup key (GLOBAL-DUP): vendor + invoice_number + total.

    This is a domain rule (what makes two invoices "the same"), not a
    generic technical helper - kept here as the single canonical copy so
    every consuming service (Decision's router, Intake, and any future
    service that needs duplicate detection) agrees on it by construction,
    not by convention.
    """
    return f"{invoice.vendor}|{invoice.invoice_number}|{invoice.total}"
