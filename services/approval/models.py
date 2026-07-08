"""Approval's own domain model: what's tracked about an escalated item
awaiting human review.

Not frozen, deliberately - mirrors services/intake/models.py's Submission:
an internal record whose status field advances over its lifetime via
model_copy(update=...) (PENDING -> WAITING_INFO/APPROVED/REJECTED). Only
cross-service wire contracts in shared/contracts/models.py are frozen
(immutable facts once published); this is a service-internal record, the
same category as Submission, not that category.

State machine (see ApprovalService for the transitions this enforces):

    PENDING ──approve──────► APPROVED
       │
       ├──reject────────────► REJECTED
       │
       └──request_info──────► WAITING_INFO ──approve──► APPROVED
                                            └─reject──► REJECTED

APPROVED/REJECTED are terminal. WAITING_INFO is not - it can still resolve
either way once more information comes back to the approver (ADR-003: once
escalated, the human owns the decision - not the AI).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from shared.contracts.models import Decision, Invoice, Recommendation


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    WAITING_INFO = "waiting_info"
    APPROVED = "approved"
    REJECTED = "rejected"


class PendingApproval(BaseModel):
    """tracking_id is the sole lookup key - not invoice id, not vendor, not
    a separate approval id. Approval never computes `recommendation` itself
    (it has no agent) - it only displays what Decision already produced;
    None exactly when Decision's own agent failed (AgentError fallback)."""

    tracking_id: str
    invoice: Invoice
    decision: Decision
    recommendation: Recommendation | None = None
    status: ApprovalStatus
