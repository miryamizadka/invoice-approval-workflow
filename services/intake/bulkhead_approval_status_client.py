"""Bulkhead (N3) around any ApprovalStatusClient - caps concurrent calls to
Approval's status-lookup endpoint and bounds their total latency, so a
slow/hung Approval Service can't tie up every Intake worker (protecting
unrelated capacity, e.g. new invoice submissions, from a degraded
dependency). Uses the same primitive
services/decision/accessors/bulkhead_llm_provider.py uses for its own
(non-Dapr) call site.
"""

from __future__ import annotations

import os

from services.intake.approval_status_client import ApprovalStatusClient, ApprovalStatusClientError
from services.intake.models import ApprovalStatusSnapshot
from shared.bulkhead import Bulkhead, BulkheadTimeoutError

_DEFAULT_MAX_CONCURRENCY = 20  # a read-only enrichment path - safe to allow
# more concurrent callers than a write path.
_DEFAULT_TIMEOUT_SECONDS = 5.0  # deliberately just above
# DaprApprovalStatusClient's own inner 3.0s timeout - under normal load the
# inner httpx timeout fires first with its own specific message; this outer
# timeout only fires under genuine semaphore starvation.


class BulkheadApprovalStatusClient:
    """ApprovalStatusClient decorator - wrapped unconditionally in
    services/intake/app.py's create_app(), including in tests: existing
    stub clients are instant (no sleep), so always-on wrapping is harmless
    and is the better property anyway - bulkhead behavior stays uniformly
    active, not silently disabled under test."""

    def __init__(
        self,
        client: ApprovalStatusClient,
        *,
        max_concurrency: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._client = client
        resolved_max_concurrency = max_concurrency
        if resolved_max_concurrency is None:
            resolved_max_concurrency = int(
                os.environ.get(
                    "APPROVAL_STATUS_BULKHEAD_MAX_CONCURRENCY", str(_DEFAULT_MAX_CONCURRENCY)
                )
            )
        resolved_timeout_seconds = timeout_seconds
        if resolved_timeout_seconds is None:
            resolved_timeout_seconds = float(
                os.environ.get(
                    "APPROVAL_STATUS_BULKHEAD_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS)
                )
            )
        self._bulkhead = Bulkhead(
            max_concurrency=resolved_max_concurrency, timeout_seconds=resolved_timeout_seconds
        )

    async def get_status(self, tracking_id: str) -> ApprovalStatusSnapshot | None:
        try:
            return await self._bulkhead.run(
                lambda: self._client.get_status(tracking_id),
                label="approval-status",
                correlation_id=tracking_id,
            )
        except BulkheadTimeoutError as exc:
            raise ApprovalStatusClientError(str(exc)) from exc
