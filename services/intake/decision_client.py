"""Intake's synchronous HTTP client for calling Decision Service directly -
a Protocol, like LLMProvider.

No longer IntakeService's primary transport: IntakeService now depends on
DecisionPublisher (services/intake/decision_publisher.py), which publishes
invoice.submitted over Dapr pub/sub and does not wait for a reply - a fire-
and-forget shape this Protocol's synchronous decide() -> Decision return
can't express (turned out to be a structural change, not just a transport
swap, once actually built - see PLAN.md). Kept, tested, and still usable
standalone: matches ARCHITECTURE.md §7's "sync invocation only where an
immediate response is required", and is convenient for local development or
manual testing against Decision Service without needing Dapr sidecars.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from shared.contracts.models import Decision, Invoice


class DecisionClientError(Exception):
    """Raised on any failure calling Decision Service - never caught silently."""


class DecisionServiceClient(Protocol):
    async def decide(
        self, invoice: Invoice, *, correlation_id: str, is_duplicate: bool = False
    ) -> Decision: ...


class HttpDecisionServiceClient:
    """No retry: POST /decisions has no idempotency-key mechanism today, so a
    naive retry-on-timeout risks invoking the LLM twice for one invoice (wasted
    cost, and a non-deterministic model could recommend differently the second
    time). Safe retry needs idempotency support on Decision Service's side
    too - deferred to when Dapr pub/sub replaces this HTTP call, which solves
    redelivery and idempotency together.
    """

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def decide(
        self, invoice: Invoice, *, correlation_id: str, is_duplicate: bool = False
    ) -> Decision:
        try:
            response = await self._client.post(
                f"{self._base_url}/decisions",
                json=invoice.model_dump(mode="json"),
                params={"is_duplicate": is_duplicate},
                headers={"X-Correlation-Id": correlation_id},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DecisionClientError(f"Decision Service call failed: {exc}") from exc
        return Decision.model_validate(response.json())
