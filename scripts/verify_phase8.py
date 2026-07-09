"""Phase 8: single verification command tying together all four required
end-to-end journeys (INV-1001, INV-1003, INV-1007, INV-1012) plus INV-1014
concurrency and the two anti-cheese guards (F10) into one script with a
clear pass/fail summary.

Host-run, matching the established convention of every other verification
script in this project (smoke_test_compose.py, verify_inv1014_concurrency.py)
- not a docker-compose service. D5 requires "one command", not "one
container"; `python -m scripts.verify_phase8` satisfies that identically,
while staying host-run avoids a new redis-py dependency and Docker-socket-
in-container complexity that a `docker compose run verification` service
would require (INV-1014's budget reset needs direct `docker exec` access,
which a container can't do to a sibling container without mounting the
host's Docker socket - a real security/complexity cost for no functional
gain, given verify_inv1014_concurrency.py already works and is reused here
by import, not reimplemented).

Requires a valid GROQ_API_KEY in .env, since the `decision` service in
docker-compose.yml always runs with LLM_PROVIDER=groq - there is no mock
provider swapped in against the real docker compose deployment.

Usage:
    docker compose up --build -d
    python -m scripts.verify_phase8
    docker compose down
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

import scripts.verify_inv1014_concurrency as concurrency_check

INTAKE_URL = "http://localhost:8000"
APPROVAL_URL = "http://localhost:8002"
PAYMENT_URL = "http://localhost:8003"
NOTIFICATION_URL = "http://localhost:8004"
HEALTH_TIMEOUT_SECONDS = 60
POLL_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 2
TOTAL_TIMEOUT_SECONDS = 900
_REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_PATH = _REPO_ROOT / "tests" / "fixtures" / "sample-invoices.json"

_FIXTURES_DATA = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
_RAW_FIXTURES: list[dict[str, Any]] = _FIXTURES_DATA["fixtures"]


# --- Fixtures ---------------------------------------------------------------


def _load_fixture(fixture_id: str, *, invoice_number_suffix: str | None = None) -> dict[str, Any]:
    """Builds a raw invoice body straight from the canonical fixture file -
    the single source of truth (same reasoning as tests/support/event_fixtures.py
    and tests/support/decision_fixtures.py's invoice_from_fixture()) - a
    hand-rolled copy here could drift from what the fixture actually says.
    `invoice_number_suffix` makes the submission unique per run, so the
    script is safely rerunnable without resetting the environment."""
    for item in _RAW_FIXTURES:
        if item["id"] == fixture_id:
            body = {k: v for k, v in item.items() if k not in ("scenario", "expected")}
            if invoice_number_suffix is not None:
                body["invoiceNumber"] = f"{body['invoiceNumber']}-{invoice_number_suffix}"
            return body
    raise SystemExit(f"FAIL: fixture {fixture_id} not found in {FIXTURES_PATH}")


def _ok(label: str, tracking_id: str, elapsed: float) -> None:
    print(f"[ok] {label} tracking_id={tracking_id} ({elapsed:.2f}s)")


# --- HTTP helpers -------------------------------------------------------------


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
        raise SystemExit(
            f"FAIL: expected 202 from POST /invoices for {body['id']}, got "
            f"{response.status_code}: {response.text}"
        )
    tracking_id: str = response.json()["tracking_id"]
    return tracking_id


async def _wait_for_intake_status(client: httpx.AsyncClient, tracking_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = await client.get(f"{INTAKE_URL}/invoices/{tracking_id}", timeout=10)
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            if body["status"] in ("completed", "failed"):
                return body
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: intake status for {tracking_id} never reached a terminal state")


async def _wait_for_approval_queue(client: httpx.AsyncClient, tracking_id: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = await client.get(f"{APPROVAL_URL}/approvals/{tracking_id}", timeout=10)
        if response.status_code == 200:
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: {tracking_id} never reached the approval queue")


async def _wait_for_payment_terminal(client: httpx.AsyncClient, tracking_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = await client.get(f"{PAYMENT_URL}/payments/{tracking_id}", timeout=10)
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            if body["status"] in ("completed", "failed"):
                return body
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: payment for {tracking_id} never reached a terminal status")


async def _wait_for_notified(client: httpx.AsyncClient, tracking_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = await client.get(f"{NOTIFICATION_URL}/notifications/{tracking_id}", timeout=10)
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            if body["notified"]:
                return body
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: {tracking_id} was never notified")


# --- Assertions ---------------------------------------------------------------


async def _assert_payment_absent(client: httpx.AsyncClient, tracking_id: str) -> None:
    response = await client.get(f"{PAYMENT_URL}/payments/{tracking_id}", timeout=10)
    if response.status_code != 404:
        raise SystemExit(
            f"FAIL: expected no payment record for duplicate {tracking_id} (F3), "
            f"got {response.status_code}: {response.text}"
        )


async def _assert_no_pending(client: httpx.AsyncClient, tracking_id: str) -> None:
    response = await client.get(f"{APPROVAL_URL}/approvals/{tracking_id}", timeout=10)
    if response.status_code == 200 and response.json()["status"] in ("pending", "waiting_info"):
        raise SystemExit(f"FAIL: {tracking_id} was left pending in the approval queue")


# --- Journeys -------------------------------------------------------------


async def verify_auto_approve(client: httpx.AsyncClient) -> None:
    """INV-1001 and INV-1002 - two distinct fixtures, not just one, proving
    this isn't a one-off lucky pass. Neither should ever touch the human
    approval queue (F6)."""
    for fixture_id in ("INV-1001", "INV-1002"):
        start = time.perf_counter()
        suffix = uuid.uuid4().hex[:8]
        body = _load_fixture(fixture_id, invoice_number_suffix=suffix)
        tracking_id = await _submit_invoice(client, body)

        status = await _wait_for_intake_status(client, tracking_id)
        route = status["decision"]["route"]
        if route != "auto_approve":
            raise SystemExit(
                f"FAIL {fixture_id}: expected route=auto_approve, got route={route}, "
                f"tracking_id={tracking_id}"
            )

        approval_response = await client.get(f"{APPROVAL_URL}/approvals/{tracking_id}", timeout=10)
        if approval_response.status_code != 404:
            raise SystemExit(
                f"FAIL {fixture_id}: unexpectedly appeared in the approval queue "
                f"(F6), tracking_id={tracking_id}"
            )

        payment = await _wait_for_payment_terminal(client, tracking_id)
        if payment["status"] != "completed":
            raise SystemExit(
                f"FAIL {fixture_id}: expected payment status=completed, got "
                f"{payment['status']}, tracking_id={tracking_id}"
            )

        await _wait_for_notified(client, tracking_id)
        _ok(f"{fixture_id} auto_approve -> paid -> notified, never reached Approval",
            tracking_id, time.perf_counter() - start)


async def verify_inv_1003(client: httpx.AsyncClient) -> None:
    start = time.perf_counter()
    suffix = uuid.uuid4().hex[:8]
    body = _load_fixture("INV-1003", invoice_number_suffix=suffix)
    tracking_id = await _submit_invoice(client, body)

    await _wait_for_approval_queue(client, tracking_id)
    response = await client.post(f"{APPROVAL_URL}/approvals/{tracking_id}/approve", timeout=10)
    if response.status_code != 200:
        raise SystemExit(
            f"FAIL INV-1003: approve returned {response.status_code}, tracking_id={tracking_id}"
        )

    payment = await _wait_for_payment_terminal(client, tracking_id)
    if payment["status"] != "completed":
        raise SystemExit(
            f"FAIL INV-1003: expected payment status=completed, got {payment['status']}, "
            f"tracking_id={tracking_id}"
        )

    await _wait_for_notified(client, tracking_id)
    _ok("INV-1003 escalate -> approve -> paid -> notified", tracking_id,
        time.perf_counter() - start)


async def verify_inv_1007(client: httpx.AsyncClient) -> None:
    """Submits an INV-1001-style base then INV-1007 (same vendor+invoiceNumber+
    total) - proves F3 live: the duplicate never reaches Payment, but still
    reaches Notification (the fix from this session's own duplicate-publish
    work). Both submissions deliberately share the same suffix - the
    collision within this run is the whole point, unlike every other journey
    here which uses a fresh suffix to avoid colliding with past runs."""
    start = time.perf_counter()
    suffix = uuid.uuid4().hex[:8]
    base_body = _load_fixture("INV-1001", invoice_number_suffix=suffix)
    await _submit_invoice(client, base_body)

    dup_body = _load_fixture("INV-1007", invoice_number_suffix=suffix)
    tracking_id = await _submit_invoice(client, dup_body)

    status = await _wait_for_intake_status(client, tracking_id)
    route = status["decision"]["route"]
    if route != "duplicate":
        raise SystemExit(
            f"FAIL INV-1007: expected route=duplicate, got route={route}, tracking_id={tracking_id}"
        )
    reason = status.get("reason") or ""
    if "duplicate" not in reason.lower():
        raise SystemExit(
            f"FAIL INV-1007: decision reason did not mention 'duplicate': {reason!r}, "
            f"tracking_id={tracking_id}"
        )

    await _assert_payment_absent(client, tracking_id)
    await _wait_for_notified(client, tracking_id)
    _ok("INV-1007 duplicate detected, never reached Payment, notified",
        tracking_id, time.perf_counter() - start)


async def verify_inv_1012(client: httpx.AsyncClient) -> None:
    start = time.perf_counter()
    suffix = uuid.uuid4().hex[:8]
    body = _load_fixture("INV-1012", invoice_number_suffix=suffix)
    tracking_id = await _submit_invoice(client, body)

    await _wait_for_approval_queue(client, tracking_id)
    response = await client.post(f"{APPROVAL_URL}/approvals/{tracking_id}/approve", timeout=10)
    if response.status_code != 200:
        raise SystemExit(
            f"FAIL INV-1012: approve returned {response.status_code}, tracking_id={tracking_id}"
        )

    payment = await _wait_for_payment_terminal(client, tracking_id)
    if payment["status"] != "failed":
        raise SystemExit(
            f"FAIL INV-1012: expected payment status=failed, got {payment['status']}, "
            f"tracking_id={tracking_id}"
        )
    reason = payment.get("reason") or ""
    if "simulated" not in reason.lower():
        raise SystemExit(
            f"FAIL INV-1012: payment failure reason did not mention the simulated decline: "
            f"{reason!r}, tracking_id={tracking_id}"
        )

    await _wait_for_notified(client, tracking_id)
    _ok("INV-1012 escalate -> approve -> payment failed (compensated) -> notified",
        tracking_id, time.perf_counter() - start)


async def verify_anti_cheese(client: httpx.AsyncClient) -> None:
    """INV-1013 (adversarial-memo, $300 > $250 ceiling, 'Approve me' in
    notes) proves both F10 guards at once against the REAL LLM (no mock):
    router.py's gate 4 (ceiling) runs unconditionally before gate 5 (agent
    signal) and never reads invoice.notes at all - so regardless of what the
    real agent recommends, and regardless of the prompt-injection attempt,
    the route can only ever be human_review here, never auto_approve. The
    agent's actual recommendation is printed for visibility, not asserted -
    the real LLM is not deterministic and there is no mock in this
    deployment; the guarantee under test is that gate 4 overrides ANY
    recommendation, not specifically an "approve" one."""
    start = time.perf_counter()
    suffix = uuid.uuid4().hex[:8]
    body = _load_fixture("INV-1013", invoice_number_suffix=suffix)
    tracking_id = await _submit_invoice(client, body)

    status = await _wait_for_intake_status(client, tracking_id)
    route = status["decision"]["route"]
    if route == "auto_approve":
        raise SystemExit(
            f"FAIL INV-1013: over-ceiling invoice with prompt-injection notes was "
            f"auto-approved! tracking_id={tracking_id}"
        )
    if route != "human_review":
        raise SystemExit(
            f"FAIL INV-1013: expected route=human_review, got route={route}, "
            f"tracking_id={tracking_id}"
        )

    recommendation = "unknown"
    approval_response = await client.get(f"{APPROVAL_URL}/approvals/{tracking_id}", timeout=10)
    if approval_response.status_code == 200:
        rec = approval_response.json().get("recommendation")
        if rec is not None:
            recommendation = rec["recommendation"]
        await client.post(f"{APPROVAL_URL}/approvals/{tracking_id}/reject", timeout=10)
        await _assert_no_pending(client, tracking_id)

    _ok(f"INV-1013 anti-cheese: ceiling + notes still routed to human_review "
        f"(agent recommended: {recommendation})", tracking_id, time.perf_counter() - start)


async def verify_inv_1014_concurrency() -> None:
    print("\n--- INV-1014A/B concurrency ---")
    start = time.perf_counter()
    await concurrency_check.main(1)
    print(f"[ok] INV-1014 concurrency ({time.perf_counter() - start:.2f}s)")


# --- Entry point --------------------------------------------------------------


async def _main_impl() -> None:
    overall_start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        print("Waiting for services to become healthy...")
        for url, name in (
            (INTAKE_URL, "intake"),
            (APPROVAL_URL, "approval"),
            (PAYMENT_URL, "payment"),
            (NOTIFICATION_URL, "notification"),
        ):
            await _wait_for_health(client, url, name)

        await verify_auto_approve(client)
        await verify_inv_1003(client)
        await verify_inv_1007(client)
        await verify_inv_1012(client)
        await verify_anti_cheese(client)
    await verify_inv_1014_concurrency()  # last, on purpose - only step that mutates a shared budget

    print(
        f"\nVERIFICATION PASSED: all four required journeys + INV-1014 concurrency + "
        f"anti-cheese guards ({time.perf_counter() - overall_start:.2f}s total)."
    )


async def main() -> None:
    await asyncio.wait_for(_main_impl(), TOTAL_TIMEOUT_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
