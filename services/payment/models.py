"""Payment's own domain models: the saga's record of a single payment
attempt, and the department budget it draws against.

Both plain (non-frozen) BaseModels, mutated via model_copy(update=...) -
same category as services/approval/models.py's PendingApproval, not the
frozen shared/contracts wire-shape category.

PaymentRecord state machine (see PaymentService for the transitions):

    (no record) --reserve budget--> RESERVED --charge succeeds--> COMPLETED
                                        |
                                        +--charge fails (compensate)--> FAILED
    (no record) --insufficient budget / unconfigured department--------> FAILED
                    (nothing was reserved - nothing to compensate)

| Current  | Event                        | Next      |
|----------|------------------------------|-----------|
| (none)   | reserve success              | RESERVED  |
| (none)   | insufficient budget          | FAILED    |
| (none)   | department not configured    | FAILED    |
| RESERVED | charge success               | COMPLETED |
| RESERVED | charge failure (compensate)  | FAILED    |
| COMPLETED/FAILED | any further event    | (no-op)   |

COMPLETED/FAILED are terminal. RESERVED is not terminal and is also the
crash-recovery resume point: if the process crashes after reserve() and
before the charge step finishes, the record is left at RESERVED, and a
redelivery of the same triggering event resumes exactly at "attempt the
charge", never re-reserving.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from shared.contracts.models import Decision, Invoice


class PaymentStatus(StrEnum):
    RESERVED = "reserved"
    COMPLETED = "completed"
    FAILED = "failed"


class PaymentRecord(BaseModel):
    """tracking_id (= Decision.correlation_id) is the sole lookup key - not
    invoice.id, not a recomputed dedup key. F3's "same invoice can't be paid
    twice" is already guaranteed upstream by Intake's dedup check before
    invoice.submitted is even published; Payment doesn't need to re-derive
    compute_dedup_key itself. tracking_id is the one identity every other
    service's repository already uses.

    reserved_amount is stored at reservation time and is what compensation
    releases - never re-derived from invoice.total at release time, so the
    two can never drift apart even if this record is read back after a
    restart."""

    tracking_id: str
    invoice: Invoice
    decision: Decision
    status: PaymentStatus
    department: str
    reserved_amount: Decimal
    reason: str | None = None  # set on terminal COMPLETED/FAILED; None while RESERVED


class Budget(BaseModel):
    """Service-internal only - never a shared contract, never crosses a
    service boundary. total is the seed value (informational); remaining is
    the live, mutated balance reserve()/release() operate on."""

    department: str
    total: Decimal
    remaining: Decimal
