# ADR-001: Autonomy Posture

**Status:** Accepted

## Context
The agent must auto-approve the low-risk majority while sending risky cases to a human. We need to decide *where the line sits* - which expenses are safe to auto-approve and which require human review. A $0 ceiling is trivially safe but defeats the product; too high a ceiling is unsafe.

## Decision
An item is auto-approved only if **all** of the following hold:
- Amount ≤ **$250** (autonomy ceiling)
- Agent confidence ≥ **0.80**
- Compliant with its category policy (e.g. SaaS ≤ $200/mo, hardware ≤ $1000)
- No hard stop (new vendor, FX, math mismatch, fraud signal, missing receipt/info)

Otherwise the item goes to human review (or reject / duplicate). The full justification is in `docs/PRODUCT-DILEMMA.md`; these thresholds are enforced by the deterministic router (see ADR-002).

## Consequences

**Pros:**
- Captures the high-volume everyday expenses (the "boring 80%").
- Conservative and safe - any single mistaken auto-approval is small and recoverable.
- Simple to reason about and defend.

**Cons:**
- Mid-range expenses ($250 up to the category limit) go to a human even when likely fine - some extra human workload.

## Alternatives considered
- **$0 ceiling** - rejected, defeats the product.
- **Per-category ceiling** - considered, deferred for simplicity (noted as a future extension).