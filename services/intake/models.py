"""Intake's own domain models: what's tracked about a submission.

Invoice/Decision are cross-service contracts (shared.contracts.models);
Submission/SubmissionStatusResponse are Intake-specific - no other service
needs to know Intake's internal tracking representation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from shared.contracts.models import Decision, Invoice


class SubmissionStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Submission(BaseModel):
    """Internal record - not what the API returns. See SubmissionStatusResponse."""

    tracking_id: str
    invoice: Invoice
    dedup_key: str
    is_duplicate: bool
    status: SubmissionStatus
    decision: Decision | None = None
    error: str | None = None


class ApprovalStatusSnapshot(BaseModel):
    """A live read of Approval Service's PendingApproval, fetched via Dapr
    service invocation (M5) - not Intake's own stored state. Deliberately
    minimal (not a full import of services.approval.models.PendingApproval -
    M3 forbids cross-service imports): just enough for a caller of
    GET /invoices/{tracking_id} to see the current approval status without
    needing to separately poll Approval Service."""

    status: str
    additional_info: str | None = None


class SubmissionStatusResponse(BaseModel):
    """What GET /invoices/{tracking_id} actually returns - deliberately not
    Submission itself. `dedup_key` is a pure internal implementation detail
    with zero value to a caller; raw `error` text could leak internal
    infrastructure details (e.g. an internal hostname/URL from an httpx
    exception) to an external API consumer. `reason` reuses Decision.reason
    when completed, or a generic safe message on failure - never the raw
    exception string.

    `approval` is populated only for a human_review decision, and only when
    the live lookup to Approval Service succeeds - None otherwise (not yet
    escalated, or the lookup degraded gracefully). Intake's own `decision`
    field never changes after Decision Service's initial verdict; `approval`
    is the one field on this model that reflects what happened *after* that,
    without Intake needing to subscribe to approval.completed itself.
    """

    tracking_id: str
    status: SubmissionStatus
    invoice: Invoice
    is_duplicate: bool
    decision: Decision | None = None
    reason: str | None = None
    approval: ApprovalStatusSnapshot | None = None

    @classmethod
    def from_submission(cls, submission: Submission) -> SubmissionStatusResponse:
        reason = submission.decision.reason if submission.decision else None
        if submission.status == SubmissionStatus.FAILED:
            reason = "Processing failed. Please contact support with the tracking id."
        return cls(
            tracking_id=submission.tracking_id,
            status=submission.status,
            invoice=submission.invoice,
            is_duplicate=submission.is_duplicate,
            decision=submission.decision,
            reason=reason,
        )
