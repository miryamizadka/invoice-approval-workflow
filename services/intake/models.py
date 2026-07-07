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


class SubmissionStatusResponse(BaseModel):
    """What GET /invoices/{tracking_id} actually returns - deliberately not
    Submission itself. `dedup_key` is a pure internal implementation detail
    with zero value to a caller; raw `error` text could leak internal
    infrastructure details (e.g. an internal hostname/URL from an httpx
    exception) to an external API consumer. `reason` reuses Decision.reason
    when completed, or a generic safe message on failure - never the raw
    exception string.
    """

    tracking_id: str
    status: SubmissionStatus
    invoice: Invoice
    is_duplicate: bool
    decision: Decision | None = None
    reason: str | None = None

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
