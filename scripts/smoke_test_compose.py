"""Manual smoke test: proves Intake and Decision talk to each other over the
Docker network (not localhost), end to end, through a real docker compose
deployment.

Not part of the pytest suite - requires `docker compose up` to already be
running (see Usage below). Requires a valid GROQ_API_KEY in .env, since the
`decision` service in docker-compose.yml always runs with LLM_PROVIDER=groq.
This intentionally does not assert a specific route (auto_approve/
human_review/etc.) - the point is proving the network/pipeline works end to
end, not re-testing LLM/router correctness (already covered by the 128
internal tests).

Usage:
    docker compose up --build -d
    python scripts/smoke_test_compose.py
    docker compose down
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

INTAKE_URL = "http://localhost:8000"
HEALTH_TIMEOUT_SECONDS = 60
PROCESSING_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 2

TEST_INVOICE = {
    "id": "SMOKE-TEST-0001",
    "submitter": "smoke-test@example.com",
    "department": "engineering-2026Q2",
    "vendor": "Smoke Test Vendor",
    "vendorKnown": True,
    "invoiceNumber": "SMOKE-0001",
    "currency": "USD",
    "category": "meals",
    "attendees": 1,
    "lineItems": [{"description": "Smoke test item", "quantity": "1", "unitPrice": "42.00"}],
    "taxAmount": "0.00",
    "total": "42.00",
    "receiptPresent": True,
    "date": "2026-05-12",
    "notes": None,
}


def _wait_for_health(url: str, name: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3) as response:
                if response.status == 200:
                    print(f"[ok] {name} healthy")
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(POLL_INTERVAL_SECONDS)
    raise SystemExit(f"FAIL: {name} did not become healthy within {timeout}s")


def _post_json(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, json.loads(response.read())


def main() -> None:
    print("Waiting for services to become healthy...")
    _wait_for_health(INTAKE_URL, "intake", HEALTH_TIMEOUT_SECONDS)
    # Decision's health isn't polled directly from the host here on purpose:
    # this script proves Intake can reach Decision *through the compose
    # network*, not merely that Decision is independently reachable.

    print("Submitting a test invoice through Intake...")
    status, body = _post_json(f"{INTAKE_URL}/invoices", TEST_INVOICE)
    if status != 202:
        raise SystemExit(f"FAIL: expected 202 from POST /invoices, got {status}: {body}")
    tracking_id = body["tracking_id"]
    print(f"[ok] submitted, tracking_id={tracking_id}")

    print("Polling until processing completes (this calls Decision Service over the network)...")
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    final_status = "received"
    while time.monotonic() < deadline:
        _, body = _get_json(f"{INTAKE_URL}/invoices/{tracking_id}")
        final_status = body["status"]
        if final_status not in ("received", "processing"):
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        raise SystemExit(
            f"FAIL: submission stuck at '{final_status}' after {PROCESSING_TIMEOUT_SECONDS}s"
        )

    if final_status != "completed":
        raise SystemExit(f"FAIL: expected status=completed, got '{final_status}': {body}")
    decision = body.get("decision")
    if decision is None:
        raise SystemExit(f"FAIL: status=completed but no decision present: {body}")
    if decision["correlation_id"] != tracking_id:
        raise SystemExit(
            f"FAIL: correlation_id {decision['correlation_id']} != tracking_id {tracking_id}"
        )

    print(f"[ok] processing completed, route={decision['route']}")
    print("\nSMOKE TEST PASSED: Intake -> Decision worked end to end over the Docker network.")


if __name__ == "__main__":
    main()
