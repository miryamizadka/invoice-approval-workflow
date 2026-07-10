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
- `PaymentGateway.charge()` takes an `idempotency_key` (the tracking id) - a redelivery-triggered resume (the RESERVED crash-recovery path documented in `PaymentRecord`) replays the original outcome instead of re-executing the charge, the same idempotent-retry principle the budget repository already applies via ETag-based optimistic concurrency, now extended to the gateway call itself. Proven directly: `PaymentService._execute_charge()` invoked twice for the same still-RESERVED record calls the gateway's underlying charge logic exactly once (`tests/unit/payment/test_service.py::test_resumed_charge_uses_tracking_id_as_idempotency_key_and_never_double_charges`).

**Cons:**
- Introduces a central coordination component (the Payment service carries this responsibility).

## Known limitation: no business/technical failure distinction at the gateway

`PaymentGatewayError` is a single exception type - every gateway failure, whether a genuine business decline or a hypothetical transient/technical failure (timeout, 503), is caught in `_execute_charge` and treated identically as terminal: compensate and mark `FAILED`. This is asymmetric with the budget repository, which does distinguish (`InsufficientBudgetError`/`BudgetNotFoundError` are business-terminal; `BudgetRepositoryError`, a genuine Dapr/infra failure, is deliberately left uncaught so Dapr redelivers).

Accepted as-is for this project's scope: there is no real payment processor here - `SimulatedPaymentGateway` is deterministic, keyed only by invoice id, and never models a transient failure at all. Modeling one would mean inventing a failure mode the project doesn't otherwise need, purely to exercise a retry path with nothing real behind it. If a real payment provider is ever integrated, this distinction (and a retry-with-backoff path for the transient case, mirroring the budget repository's existing pattern) would need to be added at that point - not before.

## Alternatives considered
- **Choreography saga** - rejected; no central coordinator makes it hard to trace and debug.
- **Two-phase commit (2PC)** - rejected; blocking and not suited to microservices (conflicts with availability / BASE).