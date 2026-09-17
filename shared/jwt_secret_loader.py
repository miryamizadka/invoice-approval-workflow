"""Externally-configurable JWT_SECRET via Dapr's Secrets API (M5/N1) - the
same fetch-once-at-startup, never-raise, resilient-fallback posture as
services/decision/service/dapr_secret_loader.py (GROQ_API_KEY), applied to
the shared authentication secret: every service that verifies a JWT
(shared/auth.py's get_current_user) and the Auth service that issues one
now read it through this loader instead of os.environ directly.

Uses secretstores.local.env (dapr/components/secretstore.yaml) - for local
docker-compose dev this is a thin indirection over the sidecar's own process
env (the same JWT_SECRET already in .env), but the app code goes through
Dapr's generic Secrets API rather than os.environ directly, so swapping to a
production secret backend (Vault, AWS Secrets Manager, k8s secrets) later
needs zero app code changes - the point of this building block.

Split into two layers on purpose (I/O vs pure logic), same reasoning as
dapr_secret_loader.py:
- load_jwt_secret_from_dapr(): the only thing that touches Dapr. Never
  raises - collapses any failure (grpc error, timeout, no value set) to
  None, which the caller treats identically to "use the JWT_SECRET env var".
- resolve_jwt_secret(): pure (str | None, str | None in -> str | None out) -
  fully unit tested without any Dapr client at all.

Callers gate the Dapr attempt behind JWT_SECRET_DAPR_ENABLED (see each
service's app.py lifespan) rather than calling this unconditionally on every
startup - DaprClient()'s constructor blocks synchronously for up to
DAPR_HEALTH_TIMEOUT (60s default) if no sidecar answers, which would hang
any test that triggers a lifespan (`with TestClient(app) as client:`)
without a real sidecar present. This mirrors dapr_secret_loader.py's own
LLM_PROVIDER=groq gate, which exists for the identical reason.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import grpc

from shared.dapr_client import LazyDaprClient

_STORE_NAME = "secretstore"
_JWT_SECRET_KEY = "JWT_SECRET"
_DEFAULT_TIMEOUT_SECONDS = 5.0

_logger = logging.getLogger(__name__)


class _SecretResponse(Protocol):
    @property
    def secret(self) -> dict[str, str]: ...


class _SecretClient(Protocol):
    async def get_secret(
        self, store_name: str, key: str, secret_metadata: dict[str, str] | None = None
    ) -> _SecretResponse: ...


async def load_jwt_secret_from_dapr(
    *, client: _SecretClient | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> str | None:
    """Never raises - None means "fall back to the JWT_SECRET env var", not
    an error, whether that's because the store is unreachable, times out, or
    simply has no value set for this key yet."""
    try:
        # Client resolution happens *inside* the try, not before it - the
        # same real bug this project already found once for GROQ_API_KEY/
        # configuration: LazyDaprClient's DaprClient() constructor does its
        # own blocking wait-for-sidecar and can itself raise TimeoutError.
        resolved_client = client or LazyDaprClient[_SecretClient]().get()
        response = await asyncio.wait_for(
            resolved_client.get_secret(_STORE_NAME, _JWT_SECRET_KEY), timeout=timeout
        )
    except (grpc.RpcError, TimeoutError) as exc:
        _logger.warning(f"dapr_jwt_secret_unreachable_using_env_fallback error={exc}")
        return None
    return response.secret.get(_JWT_SECRET_KEY)


def resolve_jwt_secret(dapr_value: str | None, env_fallback: str | None) -> str | None:
    """Pure (no I/O, no Dapr) - dapr_value wins when present and non-empty,
    otherwise env_fallback, otherwise None (callers already have their own
    "still nothing configured anywhere" handling - shared/auth.py's
    _get_secret() raises a clear RuntimeError for that case)."""
    if dapr_value:
        return dapr_value
    return env_fallback
