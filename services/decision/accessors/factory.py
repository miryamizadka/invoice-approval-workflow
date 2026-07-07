"""Selects the active LLMProvider from configuration (M13/M15) - never hardcoded."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from services.decision.accessors.groq_provider import GroqProvider
from services.decision.accessors.llm_provider import LLMProvider, LLMProviderError
from services.decision.accessors.mock_provider import MockProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {"groq": GroqProvider, "mock": MockProvider}


def get_llm_provider() -> LLMProvider:
    """Build the LLMProvider named by the LLM_PROVIDER env var (default: mock).

    Defaulting to "mock" means an unconfigured environment never makes a
    real network call by accident - LLM_PROVIDER=groq must be set explicitly.
    """
    load_dotenv()  # no-op if .env doesn't exist (e.g. Docker Compose env_file:)
    name = os.environ.get("LLM_PROVIDER", "mock").lower()
    if name not in _PROVIDERS:
        raise LLMProviderError(
            f"Unknown LLM_PROVIDER '{name}'. Valid values: {', '.join(_PROVIDERS)}."
        )
    return _PROVIDERS[name]()
