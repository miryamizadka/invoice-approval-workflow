"""N4: verifies distributed tracing is actually wired end-to-end, with
deterministic assertions against Jaeger's own HTTP API - not "open the UI
and look" (not repeatable, not hard evidence).

Drives two real journeys through the live gateway (reusing verify_phase8's
own fixture-loading/submission helpers, not duplicating them): INV-1001
(auto-approve, short - intake/decision/payment/notification) and INV-1003
(escalate-resume, longer - also exercises approval). Then queries Jaeger's
API and fails loudly if the expected services/spans aren't there.

Also checks trace continuity automatically rather than guessing: for
INV-1003, collects the set of trace IDs seen per service and reports
whether they share a common trace ID (one connected trace across the
pub/sub choreography) or not (separate per-hop traces) - printed as
evidence, not asserted in advance in any document.

Host-run, same convention as verify_phase8.py.

Usage:
    docker compose up --build -d
    python -m scripts.verify_tracing
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx

from scripts.verify_phase8 import (
    APPROVAL_URL,
    _load_fixture,
    _submit_invoice,
    _wait_for_approval_queue,
)

JAEGER_URL = "http://localhost:16686"
JAEGER_READY_TIMEOUT_SECONDS = 30
TRACE_PROPAGATION_WAIT_SECONDS = 5
EXPECTED_SERVICES = ("intake", "decision", "approval", "payment", "notification", "audit")


def _ok(message: str) -> None:
    print(f"[ok] {message}")


async def _wait_for_jaeger_reachable(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + JAEGER_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{JAEGER_URL}/api/services", timeout=3)
            if response.status_code == 200:
                _ok("Jaeger reachable")
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    raise SystemExit(
        f"FAIL: Jaeger not reachable at {JAEGER_URL} within {JAEGER_READY_TIMEOUT_SECONDS}s"
    )


async def _jaeger_services(client: httpx.AsyncClient) -> set[str]:
    response = await client.get(f"{JAEGER_URL}/api/services", timeout=10)
    if response.status_code != 200:
        raise SystemExit(
            f"FAIL: GET /api/services returned {response.status_code}: {response.text}"
        )
    return set(response.json()["data"] or [])


async def _jaeger_trace_ids_for_service(client: httpx.AsyncClient, service: str) -> set[str]:
    response = await client.get(
        f"{JAEGER_URL}/api/traces", params={"service": service, "lookback": "1h"}, timeout=10
    )
    if response.status_code != 200:
        raise SystemExit(
            f"FAIL: GET /api/traces?service={service} returned "
            f"{response.status_code}: {response.text}"
        )
    traces: list[dict[str, Any]] = response.json()["data"] or []
    return {trace["traceID"] for trace in traces}


async def verify_auto_approve_produces_spans(client: httpx.AsyncClient) -> None:
    suffix = uuid.uuid4().hex[:8]
    body = _load_fixture("INV-1001", invoice_number_suffix=suffix)
    tracking_id = await _submit_invoice(client, body)
    await asyncio.sleep(TRACE_PROPAGATION_WAIT_SECONDS)

    services = await _jaeger_services(client)
    if "intake" not in services:
        raise SystemExit(
            f"FAIL: 'intake' did not appear as a Jaeger service after submitting INV-1001 "
            f"(tracking_id={tracking_id}). Services seen: {sorted(services)}"
        )
    intake_traces = await _jaeger_trace_ids_for_service(client, "intake")
    if not intake_traces:
        raise SystemExit(f"FAIL: no spans found for 'intake' (tracking_id={tracking_id})")
    _ok(f"INV-1001: 'intake' has {len(intake_traces)} trace(s) in the last hour")


async def verify_escalate_resume_trace_continuity(client: httpx.AsyncClient) -> dict[str, set[str]]:
    """Returns the per-service trace-ID sets so main() can report continuity
    evidence, rather than asserting it here - the plan is explicit that this
    is observed and documented, not assumed."""
    suffix = uuid.uuid4().hex[:8]
    body = _load_fixture("INV-1003", invoice_number_suffix=suffix)
    tracking_id = await _submit_invoice(client, body)
    await _wait_for_approval_queue(client, tracking_id)
    response = await client.post(f"{APPROVAL_URL}/approvals/{tracking_id}/approve", timeout=10)
    if response.status_code != 200:
        raise SystemExit(
            f"FAIL: approve returned {response.status_code} for INV-1003 tracking_id={tracking_id}"
        )
    await asyncio.sleep(TRACE_PROPAGATION_WAIT_SECONDS)

    services = await _jaeger_services(client)
    missing = [s for s in EXPECTED_SERVICES if s not in services]
    if missing:
        raise SystemExit(
            f"FAIL: expected all of {EXPECTED_SERVICES} as Jaeger services after INV-1003 "
            f"(tracking_id={tracking_id}), missing: {missing}. Services seen: {sorted(services)}"
        )

    per_service_traces: dict[str, set[str]] = {}
    for service in EXPECTED_SERVICES:
        trace_ids = await _jaeger_trace_ids_for_service(client, service)
        if not trace_ids:
            raise SystemExit(f"FAIL: no spans found for '{service}' (tracking_id={tracking_id})")
        per_service_traces[service] = trace_ids
        _ok(f"INV-1003: '{service}' has {len(trace_ids)} trace(s) in the last hour")

    return per_service_traces


async def _main_impl() -> None:
    overall_start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        await _wait_for_jaeger_reachable(client)
        await verify_auto_approve_produces_spans(client)
        per_service_traces = await verify_escalate_resume_trace_continuity(client)

    shared_trace_ids = set.intersection(*per_service_traces.values())
    if shared_trace_ids:
        print(
            f"\nTRACE CONTINUITY: one connected trace across "
            f"{', '.join(EXPECTED_SERVICES)} (shared trace ID(s): {sorted(shared_trace_ids)})."
        )
    else:
        print(
            f"\nTRACE CONTINUITY: separate, disconnected per-hop traces across "
            f"{', '.join(EXPECTED_SERVICES)} (no shared trace ID found)."
        )

    print(
        f"\nVERIFICATION PASSED: all {len(EXPECTED_SERVICES)} services produced spans "
        f"({time.perf_counter() - overall_start:.2f}s total)."
    )


async def main() -> None:
    await asyncio.wait_for(_main_impl(), 300)


if __name__ == "__main__":
    asyncio.run(main())
