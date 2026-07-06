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