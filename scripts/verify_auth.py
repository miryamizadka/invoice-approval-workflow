"""N1: verifies JWT authentication + role enforcement end-to-end against
the real gateway, with deterministic assertions - not "open the UI and
look".

Two parts:
1. The Auth service itself (register/login) - always checked.
2. Role enforcement on a real protected endpoint (GET /approvals: no token
   -> 401, Submitter token -> 403, Approver token -> 200) - proves
   authorization, not just authentication. Requires Stage 2 (Depends(...)
   wired into the existing services) to be live; run this script again
   after that stage to confirm it, not just at Stage 1.

Uses a fresh, uuid-suffixed throwaway Submitter (registered via the normal
self-service flow - same collision-avoidance pattern as verify_phase8's
invoice-number suffixes) and the seeded demo Approver account
(services/auth/demo_users.json) - Approver is not self-registerable (N1
design), so there is no throwaway alternative for it.

Host-run, same convention as verify_phase8.py/verify_tracing.py.

Usage:
    docker compose up --build -d
    python -m scripts.verify_auth
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

GATEWAY_URL = "http://localhost:8080"
REACHABLE_TIMEOUT_SECONDS = 60
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEMO_USERS_PATH = _REPO_ROOT / "services" / "auth" / "demo_users.json"


def _ok(message: str) -> None:
    print(f"[ok] {message}")


def _demo_approver_credentials() -> tuple[str, str]:
    data = json.loads(_DEMO_USERS_PATH.read_text(encoding="utf-8"))
    for entry in data["users"]:
        if entry["role"] == "approver":
            return entry["email"], entry["password"]
    raise SystemExit(f"FAIL: no approver entry found in {_DEMO_USERS_PATH}")


async def _wait_for_auth_reachable(client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + REACHABLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{GATEWAY_URL}/auth/login", timeout=3)
            if response.status_code < 500:
                _ok("auth reachable")
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(2)
    raise SystemExit(
        f"FAIL: auth service not reachable through the gateway within "
        f"{REACHABLE_TIMEOUT_SECONDS}s"
    )


async def _register_throwaway_submitter(client: httpx.AsyncClient) -> tuple[str, str]:
    email = f"verify-auth-{uuid.uuid4().hex[:8]}@example.com"
    password = "verify-auth-throwaway-pass"
    response = await client.post(
        f"{GATEWAY_URL}/auth/register", json={"email": email, "password": password}, timeout=10
    )
    if response.status_code != 201:
        raise SystemExit(f"FAIL: register returned {response.status_code}: {response.text}")
    body = response.json()
    if body["role"] != "submitter":
        raise SystemExit(f"FAIL: register did not force role=submitter, got {body!r}")
    return email, password


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        f"{GATEWAY_URL}/auth/login", json={"email": email, "password": password}, timeout=10
    )
    if response.status_code != 200:
        raise SystemExit(
            f"FAIL: login for {email} returned {response.status_code}: {response.text}"
        )
    body = response.json()
    if body["token_type"] != "bearer" or not body["access_token"]:
        raise SystemExit(f"FAIL: unexpected login response shape: {body!r}")
    return str(body["access_token"])


async def verify_auth_service(client: httpx.AsyncClient) -> tuple[str, str]:
    """Returns (submitter_token, approver_token) for reuse by role-gate checks."""
    submitter_email, submitter_password = await _register_throwaway_submitter(client)
    _ok(f"registered throwaway submitter {submitter_email}")
    submitter_token = await _login(client, submitter_email, submitter_password)
    _ok("submitter login succeeded")

    approver_email, approver_password = _demo_approver_credentials()
    approver_token = await _login(client, approver_email, approver_password)
    _ok(f"seeded demo approver ({approver_email}) login succeeded")

    # Duplicate registration must be rejected.
    duplicate_response = await client.post(
        f"{GATEWAY_URL}/auth/register",
        json={"email": submitter_email, "password": "irrelevant"},
        timeout=10,
    )
    if duplicate_response.status_code != 409:
        raise SystemExit(
            f"FAIL: duplicate register expected 409, got {duplicate_response.status_code}"
        )
    _ok("duplicate register correctly rejected (409)")

    # Wrong password must be rejected, generically (anti-enumeration).
    wrong_password_response = await client.post(
        f"{GATEWAY_URL}/auth/login",
        json={"email": submitter_email, "password": "wrong"},
        timeout=10,
    )
    if wrong_password_response.status_code != 401:
        raise SystemExit(
            f"FAIL: wrong password expected 401, got {wrong_password_response.status_code}"
        )
    _ok("wrong password correctly rejected (401)")

    return submitter_token, approver_token


async def verify_role_enforcement_on_approvals(
    client: httpx.AsyncClient, submitter_token: str, approver_token: str
) -> None:
    """Proves role enforcement, not just authentication, on a real
    protected endpoint. Requires Stage 2 (Depends(require_role(...)) wired
    into services/approval/app.py) to already be live."""
    no_token_response = await client.get(f"{GATEWAY_URL}/approvals", timeout=10)
    if no_token_response.status_code != 401:
        raise SystemExit(
            f"FAIL: GET /approvals with no token expected 401, got "
            f"{no_token_response.status_code}"
        )
    _ok("GET /approvals with no token -> 401")

    submitter_response = await client.get(
        f"{GATEWAY_URL}/approvals",
        headers={"Authorization": f"Bearer {submitter_token}"},
        timeout=10,
    )
    if submitter_response.status_code != 403:
        raise SystemExit(
            f"FAIL: GET /approvals with a submitter token expected 403, got "
            f"{submitter_response.status_code}"
        )
    _ok("GET /approvals with a submitter token -> 403 (role enforced, not just authenticated)")

    approver_response = await client.get(
        f"{GATEWAY_URL}/approvals",
        headers={"Authorization": f"Bearer {approver_token}"},
        timeout=10,
    )
    if approver_response.status_code != 200:
        raise SystemExit(
            f"FAIL: GET /approvals with an approver token expected 200, got "
            f"{approver_response.status_code}"
        )
    _ok("GET /approvals with an approver token -> 200")


async def _main_impl() -> None:
    overall_start = time.perf_counter()
    async with httpx.AsyncClient() as client:
        await _wait_for_auth_reachable(client)
        submitter_token, approver_token = await verify_auth_service(client)
        await verify_role_enforcement_on_approvals(client, submitter_token, approver_token)

    print(
        f"\nVERIFICATION PASSED: auth service + role enforcement on a real endpoint "
        f"({time.perf_counter() - overall_start:.2f}s total)."
    )


async def main() -> None:
    await asyncio.wait_for(_main_impl(), 120)


if __name__ == "__main__":
    asyncio.run(main())
