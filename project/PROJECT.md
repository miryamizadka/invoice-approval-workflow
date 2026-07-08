# ApprovalFlow – Project Overview

## Purpose

ApprovalFlow is a microservice-based, AI-assisted SaaS platform for automated invoice and expense approvals.

The system receives invoice submissions asynchronously, evaluates them against a configurable company policy using an AI agent, and automatically approves only low-risk requests. Complex, risky, or high-value requests are routed to a human approver.

The system is designed around one core principle:

> **The AI agent never has authority to approve.**
> It only provides a recommendation.
> The deterministic router is the only component allowed to make an approval decision.

---

# Architecture

Main services:

- API Gateway
- Intake Service
- Decision Service
- Approval Service
- Payment Service
- Notification Service
- UI

Communication:

- External → REST
- Internal → Dapr Pub/Sub
- Saga orchestration for payments

---

# AI Design

Decision Service contains two independent parts:

1. LangGraph Agent
   - Reads invoice
   - Reads policy
   - Produces structured recommendation

2. Deterministic Router
   - Enforces autonomy policy
   - Applies thresholds
   - Applies hard stops
   - Produces the final decision

The router is the only component that can return AUTO_APPROVE.

---

# Technology Stack

- Python
- FastAPI
- LangGraph
- Dapr
- PostgreSQL
- Redis
- Docker Compose
- Pytest
- GitHub Actions

---

# Documentation

Project documentation lives under:

docs/

- architecture.md
- product-dilemma.md
- adr/

These documents are the source of truth and should never be duplicated.

---

# Current Status

Completed

- Architecture
- ADRs
- Product Dilemma
- Decision Service data models
- Deterministic Router (implementation + tests, ruff/mypy clean)
- LLM Provider Abstraction, incl. Groq strict structured-output (implementation + tests,
  ruff/mypy clean)
- LangGraph Agent (implementation + tests, ruff/mypy clean)
- Decision Service - FastAPI wrapper (`services/decision/service/`) around the agent + router,
  with a transport-agnostic `Decider` core, structured logging, correlation-id, health check,
  and a global exception handler (implementation + integration tests, ruff/mypy clean)
- shared/contracts/ - Invoice/Decision/Recommendation/enums/`compute_dedup_key` extracted from
  Decision's models into a single source both Decision and Intake depend on (pure refactor, zero
  behavior change)
- Intake Service - FastAPI wrapper (`services/intake/`) around a transport-agnostic
  `IntakeService`, with duplicate detection (F3), an in-memory repository behind a Protocol, and
  an HTTP client to Decision Service behind a Protocol (implementation + tests, ruff/mypy clean)
- Docker Compose (Phase 7 step 1) - single shared `Dockerfile`, 4-service `docker-compose.yml`
  (Intake, Decision, Postgres, Redis), verified end to end with a real `docker compose up --build`
  and `scripts/smoke_test_compose.py` (M3, M4)
- Dapr sidecars (Phase 7 step 2, M5 infra) - `daprd` sidecar per service + `placement`,
  `dapr/components/{pubsub,statestore}.yaml` backed by Redis; verified via `/v1.0/healthz`,
  `/v1.0/metadata` (components actually registered, not just "no error in the log"), and a
  re-run of `scripts/smoke_test_compose.py` proving the existing direct-HTTP Intake<->Decision
  flow is untouched. No service code changed - pub/sub and state usage are separate future steps.
- Dapr pub/sub (Phase 7 step 3, M5) - Intake and Decision now talk over real Dapr pub/sub
  (`invoice.submitted` / `decision.completed`), not direct HTTP. `IntakeService` restructured into
  a two-phase `process()`/`complete()` shape to match fire-and-forget semantics; known duplicates
  short-circuit before ever publishing (no wasted LLM call - verified the router's gate 1 never
  used the recommendation for duplicates anyway); `HttpDecisionServiceClient` kept as a tested,
  unwired standalone alternative. Verified over the real Docker network via `docker compose logs`
  (zero direct HTTP calls left between the two services) - not just `TestClient`.
- Dapr state (Phase 7 step 4, M5+M10) - `DaprStateInvoiceRepository` (Dapr state/Redis) replaces
  `InMemoryInvoiceRepository` as Intake's default, storing the full `Submission` plus a dedup
  pointer in one atomic transaction. `LazyDaprClient` extracted to `shared/dapr_client.py` and
  retrofitted into both Dapr publishers (third near-identical occurrence). Verified via a real
  `docker compose` restart that both the submission record and the dedup pointer survive - and
  found a real operational gotcha along the way: `docker compose restart <app>` alone breaks its
  `network_mode: service:<app>` sidecar's networking; `up -d --force-recreate <app> <app>-dapr` is
  the safe way to bring the pair back (documented in PLAN.md).
- Approval Service (Phase 5, F4/F5/M11) - third microservice, same layering as Intake/Decision
  (`ApprovalService`/`build_approval_service` transport-agnostic core + thin FastAPI `app.py`).
  Subscribes to `decision.completed`, ignoring every route except `human_review`; publishes
  `approval.completed` on approve/reject (not on request-info, per ADR-003). Backed by
  `DaprStateApprovalRepository` - same append-only-index pattern as Intake's repository, needed
  here because Approval must *enumerate* pending items (`GET /approvals`), which a plain
  key-by-tracking_id store can't do without a Query API. Required enriching the pre-existing
  `decision.completed` event: `Decider.decide()` now returns an internal `DecisionOutcome`
  (decision + recommendation) so Decision's subscription handler can publish invoice + decision +
  recommendation together (`DecisionCompletedEvent`) - `POST /decisions`'s external HTTP response
  is untouched (still bare `Decision`, D4). Found and fixed a real bug during design review before
  writing any code: `handle_decision_completed()` needed idempotency against Dapr's at-least-once
  redelivery - without it, a redelivered event arriving after a human already acted would have
  silently reverted the status back to `PENDING`. Verified via a real `docker compose
  up -d --force-recreate approval approval-dapr` mid-flow: both an `approved` and a `waiting_info`
  item survived with correct status, and the `waiting_info` item was still fully actionable
  (approved successfully) afterward - the central proof of durable HITL (M11).
- Payment Service (Phase 6, M9/M10, INV-1012/INV-1014) - fourth microservice, same layering
  (`PaymentService`/`build_payment_service` transport-agnostic core + thin FastAPI `app.py`).
  Orchestration-style saga (ADR-004): reserve department budget via Dapr state's ETag optimistic
  concurrency, then execute payment through a `PaymentGateway` (a `SimulatedPaymentGateway` - no
  real payment processor exists in this project, so this deterministic-decline-by-configured-id
  behavior *is* production, not a stub). On gateway failure, compensates by releasing exactly the
  amount reserved (never recomputed from the invoice), reaching `FAILED`; on success, reaches
  `COMPLETED`. A `RESERVED` intermediate status makes the saga crash-recovery-aware: a redelivered
  triggering event after a mid-saga crash resumes at the charge step rather than re-reserving,
  proven by two dedicated deterministic unit tests, not just event-redelivery idempotency (which
  it also has, mirroring Approval's exact fix). Resolved a real contradiction found in
  ARCHITECTURE.md between §8 (Payments/Budgets → PostgreSQL) and §9 (budget concurrency requires
  Dapr state with ETag) in favor of Dapr state for this phase - matches the ETag requirement
  exactly, introduces no new untested technology (no DB driver/ORM exists anywhere in this
  codebase yet), and follows the same InMemory→DaprState→Postgres migration path every prior
  service already took. Consolidated `payment.completed`/`payment.failed` into a single topic with
  a `resolution` field - the third application of the same choreography pattern already used for
  `decision.completed`/`approval.completed`. Found a genuinely new finding while reading the
  installed `starlette==1.3.1` source: `TestClient.__enter__` is the only place ASGI lifespan
  startup runs - Payment is the first service with real startup work (budget seeding), so its
  integration tests deliberately use `with TestClient(app) as client:` (every other service's
  integration tests use a bare `TestClient(app)`, which never runs lifespan hooks at all - not a
  bug there, since none of them have startup work to run). Verified live via `docker compose`:
  INV-1012's simulated gateway failure correctly compensates with the budget returning to its
  exact baseline; a real container restart of `payment`+`payment-dapr` mid-lifecycle left both the
  payment record and the department budget intact, and confirmed `ensure_seeded()` correctly did
  not reset the already-progressed budget back to its seed value; and
  `scripts/verify_inv1014_concurrency.py` (real `httpx.AsyncClient`+`asyncio.gather` concurrency,
  not sequential calls that could pass by timing luck) ran 3 iterations against the real
  Redis-backed ETag mechanism - iteration 1 showed exactly one of the INV-1014A/B pair completing
  and one failing on insufficient budget (matching the fixture's own expected math), and later
  iterations correctly showed the safety invariant (budget never negative, never oversold) holding
  even once the department's budget was too depleted for either to succeed.

In Progress

- Manual Groq strict-mode smoke-test (script ready; blocked both on the host and, now confirmed,
  inside the Docker containers by the same sandbox SSL-intercepting network egress - needs a
  network without that restriction to fully verify Groq's strict-mode structured output)

Planned

- Notification
- API Gateway / UI
- CI
- Full Phase 8 verification suite (single command, all four journeys + INV-1014 + anti-cheese)
- Demo

---

# Development Principles

- Architecture decisions are frozen.
- Follow ADRs.
- Implement one feature at a time.
- Tests before implementation whenever possible.
- Prefer deterministic logic over LLM decisions.
- Never bypass the router.