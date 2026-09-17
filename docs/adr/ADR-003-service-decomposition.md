# ADR-003: Service Decomposition

**Status:** Accepted

## Context
The system must be split into microservices. The question is *how many* and *by what criterion* - by entity (InvoiceService, UserService...) or by volatility.

## Decision
The system is decomposed into **6 components** - API Gateway, Intake, Decision, Approval, Payment, Notification - separated **by volatility and cohesion (IDesign)**, not by entity. What changes together stays together.

Two sub-decisions:
- The **agent and router live in the same service** (Decision), because they run sequentially on the same request - splitting them would create tight network coupling.
- A **send-back returns to the human** (Approval), not back through the AI, because once escalated the human owns the decision.

## Consequences

**Pros:**
- Each service has a single responsibility (SRP).
- Services can be deployed and scaled independently.
- The volatile AI is isolated in one service, away from the stable logic.

**Cons:**
- More services means more asynchronous communication and coordination to manage.

## Alternatives considered
- **Fewer services (monolith or 2–3)** - rejected; does not isolate volatility, and couples the AI with stable logic.
- **Splitting agent and router into two services** - rejected; they always run together, so this only adds network coupling (chatty).

## Update (post-acceptance, not a revision of the original decision above)
Three services were added after this ADR was accepted: Audit (F9/ADR-008), UI (M7/ADR-010), and Auth
(N1). None of them contradict the volatility-based decomposition principle above - each was added
because it changes for its own distinct reason, separate from the original six:
- **Audit** and **UI** are domain services in the same sense as the original six (Audit projects a
  read model per ADR-008; UI is a logic-free static-file mount per ADR-010) - they simply postdate
  this ADR's original list, which was never updated when they were added.
- **Auth** is different in kind, not just a missed update: it's cross-cutting infrastructure (the
  same category as the Gateway's routing/rate-limiting), not a use-case-orchestrating Manager - see
  `ARCHITECTURE.md` §5/§12 for the full argument. It's implemented as a full service (not gateway
  config) only because JWT issuance/verification and password hashing need real logic and a user
  store.
- **Bulkhead/Throttling (N3)** added no new services at all - `shared/bulkhead.py`/
  `shared/rate_limiter.py` are cross-cutting wrappers around existing Accessor/Manager call sites,
  constructed at the same sites that already existed.

This note intentionally doesn't rewrite the "6 components" line above - it records what changed and
why, without erasing the original decision as it was made.