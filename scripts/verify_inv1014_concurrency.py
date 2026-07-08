"""Manual verification: proves the INV-1014A/B budget-concurrency guarantee
over the real Docker network - a real DaprStateBudgetRepository backed by
Redis, racing two genuinely concurrent HTTP requests, not a unit test with
a fake client.

This is REQUIRED verification for Payment Service (Phase 6), not optional:
a manual curl-based demo could "pass" purely by timing luck even if the
underlying ETag guarantee were broken. This script uses httpx.AsyncClient +
asyncio.gather so the two `approve` calls actually overlap in time, and runs
several iterations (not once) to build real confidence rather than a single
lucky run.

Requires `docker compose up --build -d` already running (9+2 = 11
containers, including `payment`/`payment-dapr`). Each iteration submits a
fresh INV-1014-style pair (unique invoice numbers, to dodge F3 dedup)
against the real `marketing-2026Q2` department budget. Before each
iteration, the script resets that budget directly in Redis (via `docker
exec`, never through an HTTP endpoint - Payment deliberately exposes no
"reset budget" API; adding one purely for test convenience would be a real
operational hazard in a service that manages money) so every iteration is
a genuine race against a fresh $1000, not just the first one. This was
verified empirically, not assumed: Dapr's Redis state store keys are
`{app-id}||{key}` HASHes with `data`/`version` fields (`version` doubles as
the ETag) - confirmed via `redis-cli HGETALL`. Resetting only the `data`
field leaves Dapr's own version/etag bookkeeping untouched, so the
reserve()/release() ETag mechanism keeps working normally afterward.

Usage:
    docker compose up --build -d
    python scripts/verify_inv1014_concurrency.py [iterations]
    docker compose down
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx

INTAKE_URL = "http://localhost:8000"
APPROVAL_URL = "http://localhost:8002"
PAYMENT_URL = "http://localhost:8003"
HEALTH_TIMEOUT_SECONDS = 60
POLL_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 2
DEFAULT_ITERATIONS = 3
DEPARTMENT = "marketing-2026Q2"
DEPARTMENT_SEED_TOTAL = Decimal("1000.00")
REDIS_CONTAINER = "invoice-approval-workflow-redis-1"
REDIS_STATE_KEY = f"payment||budget:{DEPARTMENT}"


def _reset_department_budget() -> None:
    """Test-only: overwrites the budget's Redis-stored `data` field directly,
    bypassing the app entirely (there is no, and should be no, HTTP endpoint
    for this). Leaves Dapr's own `version` field untouched."""
    seed_json = json.dumps(
        {
            "department": DEPARTMENT,
            "total": str(DEPARTMENT_SEED_TOTAL),
            "remaining": str(DEPARTMENT_SEED_TOTAL),
        }
    )
    result = subprocess.run(
        [
            "docker", "exec", REDIS_CONTAINER,
            "redis-cli", "HSET", REDIS_STATE_KEY, "data", seed_json,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"FAIL: could not reset {DEPARTMENT} budget in Redis: {result.stderr}")


def _invoice_body(suffix: str, invoice_number_suffix: str) -> dict[str, Any]:
    return {
        "id": f"INV-1014-{suffix}",
        "submitter": "marketing.lead@northwind.example",
        "department": DEPARTMENT,
        "vendor": "ExpoWorks",
        "vendorKnown": True,
        "invoiceNumber": f"EW-{invoice_number_suffix}",
        "currency": "USD",
        "category": "other",
        "lineItems": [{"description": "Conference booth", "quantity": "1", "unitPrice": "600.00"}],
        "taxAmount": "0.00",
        "total": "600.00",
        "receiptPresent": True,
        "date": "2026-05-18",
        "notes": None,
    }


async def _wait_for_health(client: httpx.AsyncClient, url: str, name: str) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{url}/health", timeout=3)
            if response.status_code == 200:
                print(f"[ok] {name} healthy")
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: {name} did not become healthy within {HEALTH_TIMEOUT_SECONDS}s")


async def _submit_invoice(client: httpx.AsyncClient, body: dict[str, Any]) -> str:
    response = await client.post(f"{INTAKE_URL}/invoices", json=body, timeout=10)
    if response.status_code != 202:
        raise SystemExit(f"FAIL: expected 202 from POST /invoices, got {response.status_code}")
    tracking_id: str = response.json()["tracking_id"]
    return tracking_id


async def _wait_for_approval_queue(client: httpx.AsyncClient, tracking_id: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = await client.get(f"{APPROVAL_URL}/approvals/{tracking_id}", timeout=10)
        if response.status_code == 200:
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: {tracking_id} never reached the approval queue")


async def _approve(client: httpx.AsyncClient, tracking_id: str) -> int:
    response = await client.post(f"{APPROVAL_URL}/approvals/{tracking_id}/approve", timeout=10)
    return response.status_code


async def _wait_for_payment_terminal(client: httpx.AsyncClient, tracking_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = await client.get(f"{PAYMENT_URL}/payments/{tracking_id}", timeout=10)
        if response.status_code == 200:
            body = response.json()
            if body["status"] in ("completed", "failed"):
                return dict(body)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: payment for {tracking_id} never reached a terminal status")


async def _get_budget_remaining(client: httpx.AsyncClient) -> Decimal:
    response = await client.get(f"{PAYMENT_URL}/budgets/{DEPARTMENT}", timeout=10)
    if response.status_code != 200:
        raise SystemExit(f"FAIL: could not read budget for {DEPARTMENT}: {response.status_code}")
    return Decimal(response.json()["remaining"])


async def _run_iteration(client: httpx.AsyncClient, iteration: int) -> None:
    print(f"\n--- iteration {iteration} ---")
    _reset_department_budget()
    remaining_before = await _get_budget_remaining(client)
    print(f"[ok] {DEPARTMENT} reset to a fresh {remaining_before} for this iteration")

    suffix = uuid.uuid4().hex[:8]
    body_a = _invoice_body(f"A-{iteration}", f"7001-{suffix}")
    body_b = _invoice_body(f"B-{iteration}", f"7002-{suffix}")

    tid_a = await _submit_invoice(client, body_a)
    tid_b = await _submit_invoice(client, body_b)
    print(f"[ok] submitted pair: {tid_a}, {tid_b}")

    await _wait_for_approval_queue(client, tid_a)
    await _wait_for_approval_queue(client, tid_b)

    # The actual race: both `approve` calls fired concurrently, not sequentially.
    status_a, status_b = await asyncio.gather(
        _approve(client, tid_a), _approve(client, tid_b)
    )
    print(f"[ok] approve responses: a={status_a}, b={status_b}")

    payment_a = await _wait_for_payment_terminal(client, tid_a)
    payment_b = await _wait_for_payment_terminal(client, tid_b)
    remaining_after = await _get_budget_remaining(client)

    statuses = {payment_a["status"], payment_b["status"]}
    if remaining_after < 0:
        raise SystemExit(f"FAIL: {DEPARTMENT} remaining went negative: {remaining_after}")

    if statuses == {"completed", "failed"}:
        print(
            f"[ok] exactly one succeeded, one failed (insufficient budget) - "
            f"remaining={remaining_after}"
        )
    else:
        raise SystemExit(
            f"FAIL: unexpected outcome against a fresh {remaining_before} budget - "
            f"expected exactly one success and one failure, got: "
            f"{payment_a['status']}, {payment_b['status']}"
        )


async def main(iterations: int) -> None:
    async with httpx.AsyncClient() as client:
        print("Waiting for services to become healthy...")
        await _wait_for_health(client, APPROVAL_URL, "approval")
        await _wait_for_health(client, PAYMENT_URL, "payment")

        for i in range(1, iterations + 1):
            await _run_iteration(client, i)

    print(f"\nVERIFICATION PASSED: {iterations} iteration(s), budget never oversold or negative.")


if __name__ == "__main__":
    requested_iterations = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ITERATIONS
    asyncio.run(main(requested_iterations))
