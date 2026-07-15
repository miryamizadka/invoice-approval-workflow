"""Shared login helpers for the host-run verification scripts that predate
N1 (verify_phase8.py, verify_tracing.py, verify_inv1014_concurrency.py) -
every endpoint they call is now auth-gated, so each needs a Submitter,
Approver, and Admin token. Extracted here once rather than duplicated
across all three; services/auth/demo_users.json stays the single source of
truth for the seeded Approver/Admin credentials (same file verify_auth.py
already reads).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

GATEWAY_URL = "http://localhost:8080"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEMO_USERS_PATH = _REPO_ROOT / "services" / "auth" / "demo_users.json"


@dataclass(frozen=True)
class AuthTokens:
    submitter: str
    approver: str
    admin: str

    def header(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


def _demo_credentials(role: str) -> tuple[str, str]:
    data = json.loads(_DEMO_USERS_PATH.read_text(encoding="utf-8"))
    for entry in data["users"]:
        if entry["role"] == role:
            return str(entry["email"]), str(entry["password"])
    raise SystemExit(f"FAIL: no {role} entry found in {_DEMO_USERS_PATH}")


async def _register_throwaway_submitter(client: httpx.AsyncClient) -> tuple[str, str]:
    email = f"verify-{uuid.uuid4().hex[:8]}@example.com"
    password = "verify-throwaway-pass"
    response = await client.post(
        f"{GATEWAY_URL}/auth/register", json={"email": email, "password": password}, timeout=10
    )
    if response.status_code != 201:
        raise SystemExit(f"FAIL: register returned {response.status_code}: {response.text}")
    return email, password


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        f"{GATEWAY_URL}/auth/login", json={"email": email, "password": password}, timeout=10
    )
    if response.status_code != 200:
        raise SystemExit(
            f"FAIL: login for {email} returned {response.status_code}: {response.text}"
        )
    return str(response.json()["access_token"])


async def acquire_tokens(client: httpx.AsyncClient) -> AuthTokens:
    """Registers a fresh throwaway Submitter and logs in as it, plus the two
    seeded demo accounts (Approver, Admin - neither is self-registerable per
    the N1 design, so the seed file is the only source for them)."""
    submitter_email, submitter_password = await _register_throwaway_submitter(client)
    submitter_token = await _login(client, submitter_email, submitter_password)

    approver_email, approver_password = _demo_credentials("approver")
    approver_token = await _login(client, approver_email, approver_password)

    admin_email, admin_password = _demo_credentials("admin")
    admin_token = await _login(client, admin_email, admin_password)

    return AuthTokens(submitter=submitter_token, approver=approver_token, admin=admin_token)
