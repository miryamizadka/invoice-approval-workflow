"""Intake's Dapr service-invocation client for Approval Service's live status
(M5) - the one synchronous cross-service call in this project.

Every other service-to-service data flow here is deliberately asynchronous
(Dapr Pub/Sub): Approval/Payment/Audit all embed a full Invoice snapshot in
the events they consume specifically to avoid needing a call back to another
service. The one real, narrow gap that approach doesn't close: Intake's own
`GET /invoices/{tracking_id}` freezes at Decision Service's original verdict,
since Intake never subscribes to approval.completed. This client closes that
gap by calling Approval's already-existing `GET /approvals/{tracking_id}`
through Dapr, rather than Intake also subscribing to a new event stream.

Calls the local Dapr sidecar's HTTP invoke API
(http://localhost:{DAPR_HTTP_PORT}/v1.0/invoke/<app-id>/method/<path>), not
the Python SDK's `invoke_method` - that method is gRPC-based, deprecated
upstream, and (confirmed by reading the installed dapr SDK's source) never
surfaces the callee's real HTTP status code, which this needs in order to
tell a 404 ("not escalated yet") apart from a genuine failure.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx
from pydantic import ValidationError

from services.intake.models import ApprovalStatusSnapshot

_DEFAULT_TIMEOUT_SECONDS = 3.0  # named, not a bare literal - matches
# dapr_config_loader.py's _DEFAULT_TIMEOUT_SECONDS convention. Short: this
# blocks a synchronous GET /invoices/{id} caller, not a background job.


class ApprovalStatusClientError(Exception):
    """Raised on any unexpected failure calling Approval Service - never
    caught silently. A 404 is not this - see get_status()'s return of None."""


class ApprovalStatusClient(Protocol):
    async def get_status(self, tracking_id: str) -> ApprovalStatusSnapshot | None: ...


class DaprApprovalStatusClient:
    """No retry, no circuit breaker - one attempt, then the caller degrades
    gracefully (see IntakeService.get_status_response). This is a read-only
    GET-enrichment, not a saga step that needs resilience against a transient
    failure at all costs."""

    def __init__(
        self,
        *,
        dapr_http_port: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        port = dapr_http_port or os.environ.get("DAPR_HTTP_PORT", "3500")
        self._base_url = f"http://localhost:{port}/v1.0/invoke/approval/method"
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def get_status(self, tracking_id: str) -> ApprovalStatusSnapshot | None:
        try:
            response = await self._client.get(
                f"{self._base_url}/approvals/{tracking_id}",
                headers={"X-Correlation-Id": tracking_id},
            )
        except httpx.HTTPError as exc:
            raise ApprovalStatusClientError(f"Approval Service invocation failed: {exc}") from exc
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ApprovalStatusClientError(f"Approval Service invocation failed: {exc}") from exc
        try:
            return ApprovalStatusSnapshot.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            # Covers both invalid JSON (ValueError from response.json()) and
            # a well-formed-but-wrong-shape body (ValidationError) - one
            # error type for every parsing failure, not a raw KeyError
            # leaking past this client.
            raise ApprovalStatusClientError(
                f"Approval Service returned an unexpected body: {exc}"
            ) from exc
