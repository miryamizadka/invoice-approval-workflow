"""Externally-configurable GROQ_API_KEY via Dapr's Secrets API (M5) - the
same fetch-once-at-startup, never-raise, resilient-fallback posture as
dapr_config_loader.py (F7/M13), applied to a genuine secret instead of a
policy value.

Uses secretstores.local.env (dapr/components/secretstore.yaml) - for local
docker-compose dev this is a thin indirection over the sidecar's own process
env (the same GROQ_API_KEY already in .env), but the app code goes through
Dapr's generic Secrets API rather than os.environ directly, so swapping to a
production secret backend (Vault, AWS Secrets Manager, k8s secrets) later
needs zero app code changes - the point of this building block.

Split into two layers on purpose (I/O vs pure logic), same reasoning as
dapr_config_loader.py:
- load_groq_api_key(): the only thing that touches Dapr. Never raises -
  collapses any failure (grpc error, timeout, no value set) to None, which
  the caller treats identically to "use the existing env-var-based provider".
- resolve_provider_from_secret(): pure (str | None, LLMProvider in -> LLMProvider
  out) - fully unit tested without any Dapr client at all.

Provider priority (explicit, to avoid future confusion): (1) a `provider`
injected explicitly by a caller/test always wins, this module is never even
consulted; (2) when LLM_PROVIDER=groq, a valid Dapr secret if available;
otherwise (3) the env var GROQ_API_KEY (existing, unchanged behavior). "Mock"
is not a fourth fallback tier below env - it's an orthogonal LLM_PROVIDER
value, never attempting a secret/groq lookup at all.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import grpc

from services.decision.accessors.groq_provider import GroqProvider
from services.decision.accessors.llm_provider import LLMProvider, LLMProviderError
from shared.dapr_client import LazyDaprClient

_STORE_NAME = "secretstore"
_GROQ_API_KEY_SECRET = "GROQ_API_KEY"
_DEFAULT_TIMEOUT_SECONDS = 5.0

_logger = logging.getLogger(__name__)


class _SecretResponse(Protocol):
    @property
    def secret(self) -> dict[str, str]: ...


class _SecretClient(Protocol):
    async def get_secret(
        self, store_name: str, key: str, secret_metadata: dict[str, str] | None = None
    ) -> _SecretResponse: ...


async def load_groq_api_key(
    *, client: _SecretClient | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> str | None:
    """Never raises - None means "use the existing env-var-based provider",
    not an error, whether that's because the store is unreachable, times
    out, or simply has no value set for this key yet."""
    try:
        # Client resolution happens *inside* the try, not before it - same
        # real bug this project already found once for configuration:
        # LazyDaprClient's DaprClient() constructor does its own blocking
        # wait-for-sidecar (up to 60s) and eventually raises TimeoutError.
        resolved_client = client or LazyDaprClient[_SecretClient]().get()
        response = await asyncio.wait_for(
            resolved_client.get_secret(_STORE_NAME, _GROQ_API_KEY_SECRET), timeout=timeout
        )
    except (grpc.RpcError, TimeoutError) as exc:
        _logger.warning(f"dapr_secret_unreachable_using_env_fallback error={exc}")
        return None
    return response.secret.get(_GROQ_API_KEY_SECRET)


def resolve_provider_from_secret(secret_key: str | None, fallback: LLMProvider) -> LLMProvider:
    """Pure (no I/O, no Dapr) - given load_groq_api_key()'s result, decide the
    active provider. Mirrors merge_thresholds()'s split from _fetch_raw_config:
    the I/O half is untestable-without-a-fake-client and already covered;
    this half is pure and gets full unit test coverage instead of only being
    live-verified.

    Named "resolve", not "build" (unlike build_decider/build_intake_service/
    build_approval_service elsewhere in this codebase) - those always
    unconditionally construct; this sometimes returns `fallback` unchanged,
    a conditional selection, not a composition seam."""
    if not secret_key:
        return fallback
    try:
        return GroqProvider(api_key=secret_key)
    except LLMProviderError as exc:
        _logger.warning(f"dapr_secret_invalid_using_fallback error={exc}")
        return fallback
