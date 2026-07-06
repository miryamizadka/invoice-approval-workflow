# ADR-006: Dapr as Integration Layer

**Status:** Accepted

## Context
Microservices need inter-service messaging, durable state, secrets, and externally configurable policy - without hard-coding any specific infrastructure into the services.

## Decision
Use **Dapr** as the integration layer over **Redis**, covering three needs with one consistent API:
- **Pub/sub** for asynchronous service-to-service events.
- **State store** for durable data: the HITL paused/resume state (M11), idempotency and dedup keys (M10), and budget reservations with optimistic concurrency (INV-1014).
- **Configuration & secrets** for externally changeable policy and thresholds (M13) and the LLM keys (M15).

## Consequences

**Pros:**
- Services depend on Dapr's API, not on Redis directly — the backing store is swappable (DIP).
- Durable state survives container restarts, enabling durable HITL (M11).
- Policy changes take effect with no redeploy (M13).

**Cons:**
- Adds Dapr as a runtime dependency (a sidecar per service) and a learning curve.

## Alternatives considered
- **Direct Redis / broker SDKs in each service** - rejected; couples services to specific infrastructure.
- **Separate tools for each concern** (broker + config service + secrets manager) — rejected; Dapr unifies them behind one API.