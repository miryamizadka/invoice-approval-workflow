# ADR-002: Agent Recommends, Router Enforces (M12)

**Status:** Accepted

## Context
We must prove the system can never auto-approve above the configured ceiling (M12). This cannot be guaranteed with a probabilistic AI agent alone.

## Decision
The AI agent only *recommends* (recommendation + confidence + cited rules). A deterministic router enforces the autonomy rules and is the single code path that can issue AUTO_APPROVE.

## Consequences

**Pros:**
- Provable ceiling (M12), resistant to prompt-steering (anti-cheese), fully auditable.

**Cons:**
- An extra layer in the decision flow; the agent is less autonomous (recommends, doesn't decide).

## Alternatives considered
- Fully autonomous agent - rejected, not provable.
- Confidence-only gating - rejected, confidence is itself probabilistic.