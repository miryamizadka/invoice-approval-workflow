"""Manual smoke test: real Groq strict structured output.

Not part of the pytest suite and not run in CI (ADR-005: CI stays fully
mocked/stubbed). Run this by hand, with a real GROQ_API_KEY in .env and
real network access to api.groq.com, to validate that openai/gpt-oss-120b
with strict:true actually honors the Recommendation schema end to end -
including the numeric confidence bounds (minimum/maximum), which Groq's
public docs don't explicitly confirm as a supported strict-mode keyword.

Usage (from the repo root):
    python -m scripts.smoke_test_groq_strict
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from services.decision.accessors.groq_provider import GroqProvider  # noqa: E402
from services.decision.agent.prompts import SYSTEM_PROMPT  # noqa: E402
from shared.contracts.models import Recommendation  # noqa: E402

load_dotenv()

USER_MESSAGE = """Policy:
MEAL-01: Personal/team meals are reimbursable up to $75 per attendee.
GLOBAL-VENDOR: A new/unknown vendor is always reviewed by a human.
AUTONOMY-CEILING: The agent may auto-approve only when the USD amount is <= $250.

Invoice to evaluate:
{
  "vendor": "Bistro 19",
  "vendor_known": true,
  "category": "meals",
  "attendees": 1,
  "total": "42.00",
  "currency": "USD",
  "receipt_present": true,
  "notes": "Solo working lunch."
}
"""


async def main() -> None:
    provider = GroqProvider()
    raw = await provider.complete(SYSTEM_PROMPT, USER_MESSAGE, schema=Recommendation)
    print("RAW RESPONSE:\n", raw)
    recommendation = Recommendation.model_validate_json(raw)
    print("\nPARSED OK:", recommendation)
    if not 0.0 <= recommendation.confidence <= 1.0:
        raise SystemExit(f"FAIL: confidence {recommendation.confidence} is out of [0, 1] bounds!")
    print("\nconfidence within [0,1]: OK - strict mode's minimum/maximum risk is cleared.")


if __name__ == "__main__":
    asyncio.run(main())
