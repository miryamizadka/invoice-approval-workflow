"""Loads the policy text the agent prompts with. A local file today; the
loading mechanism is the seam M13's external config would replace later -
callers only depend on getting a str back, not on how it's sourced."""

from __future__ import annotations

from pathlib import Path

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[3] / "policy" / "policy.md"


def load_policy_text(path: Path = DEFAULT_POLICY_PATH) -> str:
    return path.read_text(encoding="utf-8")
