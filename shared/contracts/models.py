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
    """Structured invoice data as consumed from the invoice.submitted event."""

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


def compute_dedup_key(invoice: Invoice) -> str:
    """Pure dedup key (GLOBAL-DUP): vendor + invoice_number + total.

    This is a domain rule (what makes two invoices "the same"), not a
    generic technical helper - kept here as the single canonical copy so
    every consuming service (Decision's router, Intake, and any future
    service that needs duplicate detection) agrees on it by construction,
    not by convention.
    """
    return f"{invoice.vendor}|{invoice.invoice_number}|{invoice.total}"
