"""Tests for DaprApprovalStatusClient - Intake's Dapr service-invocation call
to Approval Service's GET /approvals/{tracking_id}, via the local sidecar's
HTTP invoke API (M5). Uses httpx.MockTransport - no real Dapr sidecar, no
real Approval Service, same technique as test_decision_client.py.
"""

from __future__ import annotations

import httpx
import pytest

from services.intake.approval_status_client import (
    ApprovalStatusClientError,
    DaprApprovalStatusClient,
)
from services.intake.models import ApprovalStatusSnapshot


def _client_returning(handler) -> DaprApprovalStatusClient:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    return DaprApprovalStatusClient(client=async_client)


async def test_get_status_returns_snapshot_on_200() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"status": "pending", "additional_info": None})

    client = _client_returning(handler)

    result = await client.get_status("corr-1")

    assert result == ApprovalStatusSnapshot(status="pending", additional_info=None)
    request = captured["request"]
    assert request.url.path == "/v1.0/invoke/approval/method/approvals/corr-1"
    assert request.headers["X-Correlation-Id"] == "corr-1"


async def test_get_status_returns_additional_info_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "pending", "additional_info": "more details"})

    client = _client_returning(handler)

    result = await client.get_status("corr-1")

    assert result == ApprovalStatusSnapshot(status="pending", additional_info="more details")


async def test_get_status_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "tracking_id not found"})

    client = _client_returning(handler)

    result = await client.get_status("missing")

    assert result is None


async def test_get_status_raises_on_500() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal_error"})

    client = _client_returning(handler)

    with pytest.raises(ApprovalStatusClientError):
        await client.get_status("corr-1")


async def test_get_status_raises_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client_returning(handler)

    with pytest.raises(ApprovalStatusClientError):
        await client.get_status("corr-1")


async def test_get_status_raises_on_malformed_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = _client_returning(handler)

    with pytest.raises(ApprovalStatusClientError):
        await client.get_status("corr-1")
