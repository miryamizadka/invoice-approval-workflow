# ADR-004: Saga Orchestration

**Status:** Accepted

## Context
The payment flow spans multiple steps (reserve department budget, then execute payment). If one step fails mid-way, we must guarantee a consistent outcome - no orphaned reservation, no partial or double payment. There is no single ACID transaction across services.

## Decision
Use a **Saga with compensation**, in the **orchestration** style: the Payment service acts as the central coordinator that runs the steps forward and, on failure, runs the compensating actions in reverse. Every forward step has an opposite compensating action (reserve ↔ release).

## Consequences

**Pros:**
- Guaranteed consistent outcome - either the payment completes, or every partial effect is undone.
- Easy to monitor and debug - a central coordinator makes it clear which step the flow is on.
- Explicit, readable compensation logic.

**Cons:**
- Introduces a central coordination component (the Payment service carries this responsibility).

## Alternatives considered
- **Choreography saga** - rejected; no central coordinator makes it hard to trace and debug.
- **Two-phase commit (2PC)** - rejected; blocking and not suited to microservices (conflicts with availability / BASE).