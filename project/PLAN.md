# ApprovalFlow – Implementation Plan

## Mission

Deliver a working, production-quality AI-assisted invoice approval platform.

The priority is not implementing every possible feature.

The priority is:

1. Correct business flow
2. Deterministic AI decision architecture
3. Four required end-to-end journeys
4. Must-have architecture requirements
5. Quality and documentation

---

# Current Status

## Completed

- [x] Architecture design
- [x] ARCHITECTURE.md
- [x] ADRs
- [x] Product Dilemma
- [x] PROJECT.md
- [x] CLAUDE.md
- [x] MASTER_CHECKLIST.md
- [x] Decision Service domain models
- [x] Deterministic Router (services/decision/router/)
- [x] LLM provider abstraction (services/decision/accessors/)
- [x] LangGraph agent (services/decision/agent/) - Phase 3 done, pending the manual Groq
  smoke-test (see Phase 3 tasks below)
- [x] Decision Service (services/decision/service/) - Phase 2 done: FastAPI wrapping the agent
  graph + router, with a clean `Decider.decide()` transport-agnostic core (see Phase 2 below)
- [x] shared/contracts/ (Invoice, LineItem, Decision, Recommendation, enums, compute_dedup_key)
  extracted from services/decision/models.py - both Decision and Intake depend on this single
  canonical source, neither imports the other's codebase. Pure refactor: all 113 pre-existing
  tests passed unmodified before Intake was built on top of it.
- [x] Intake Service (services/intake/) - Phase 4 done: FastAPI wrapping a clean `IntakeService`
  core, `InMemoryInvoiceRepository` behind a Protocol, `HttpDecisionServiceClient` behind a
  Protocol (see Phase 4 below)
- [x] Docker Compose environment (Phase 7, step 1) - single shared `Dockerfile`, 4-service
  `docker-compose.yml` (intake, decision, postgres, redis), verified with a real
  `docker compose up --build` and `scripts/smoke_test_compose.py` (see Phase 7 below)
- [x] Dapr pub/sub between Intake and Decision (Phase 7, step 3)
- [x] Dapr state for Intake's `InvoiceRepository` (Phase 7, step 4)
- [x] Approval Service (Phase 5) - F4 (queue + display), F5 (approve/reject/request-info), M11
  (durable pause/resume) - see Phase 5 below
- [x] Payment Service (Phase 6) - M9 (Saga + Compensation), M10 (Idempotency), INV-1012 (payment
  failure + compensation), INV-1014A/B (budget concurrency) - see Phase 6 below

## Current Focus

Phase 1-6, and Phase 7 steps 1/3/4 (Docker Compose, Dapr pub/sub, Dapr state) are all complete and
verified. Remaining before Phase 3 is fully "production-ready": run
`scripts/smoke_test_groq_strict.py` manually against the real Groq API from a network that isn't
behind an SSL-intercepting proxy (confirmed blocked both on the host and inside Docker containers -
environmental, not a code issue). Next: Notification Service (pure consumer of `payment.completed`,
notifies the submitter - F2/M8) and the API Gateway/UI (M6/M7), then Phase 8's full verification
suite tying all four required journeys (INV-1001, INV-1003, INV-1007, INV-1012) plus INV-1014
together into one command.

---

# Phase 1 — Decision Engine (CRITICAL)

## Goal

Build the core intelligence of ApprovalFlow.

The system must prove:

- AI recommends only.
- Router decides.
- The autonomy posture cannot be violated.

## Tasks

- [x] Define invoice domain model
- [x] Define AI recommendation model
- [x] Define decision result model

- [x] Complete Router implementation (`services/decision/router/`)

Rules:

- [x] Amount ceiling enforcement (AUTONOMY-CEILING, $250)
- [x] Confidence threshold enforcement (AUTONOMY-CONFIDENCE, 0.80)
- [x] Hard stops enforcement (GLOBAL-VENDOR, GLOBAL-FX, GLOBAL-RECEIPT, GLOBAL-MATH, MEAL-01 missing-info)
- [x] Duplicate detection (gate 1, via externally-supplied `is_duplicate`)
- [x] Category policy enforcement (MEAL-01, SAAS-01, HW-02, TRAVEL-02 caps)

- [x] Complete Router unit tests (69 tests: `tests/unit/decision/test_router.py` +
  `test_router_boundaries.py`)

Required scenarios:

- [x] Low-risk invoice → AUTO_APPROVE
- [x] Amount above ceiling → HUMAN_REVIEW
- [x] Low confidence → HUMAN_REVIEW
- [x] New vendor → HUMAN_REVIEW
- [x] Fraud flag → HUMAN_REVIEW (via agent ESCALATE citing GLOBAL-FRAUD; deterministic
  fraud-pattern heuristics are an explicit, documented future-scope item, not implemented here)
- [x] Duplicate → DUPLICATE

- [x] Add anti-cheese test

Scenario:

Agent recommends approval for a $5000 invoice.

Expected:

Router rejects AUTO_APPROVE.

Verified (`test_m12_ceiling_survives_optimistic_agent` + the generalized
`test_m12_no_fixture_can_be_forced_to_auto_approve` across all applicable fixtures).

Known, documented scope limit: MEAL-03 (alcohol-only → REJECT) has no structural Invoice
field and is agent-judged by design; `test_m12_scope_limit_meal_03_reject_can_be_bypassed_by_a_wrong_agent`
proves and records this explicitly rather than hiding it. MEAL-02 (missing client name/justification)
and TRAVEL-03 (travel class) are similarly deferred - no structured fields exist for them yet.

---

# Phase 2 — Decision Service

## Goal

Expose decision capability as a real service.

## Tasks

- [x] Create Decision Service API (`services/decision/service/`: `Decider`/`build_decider`
  transport-agnostic core + `app.py` FastAPI wrapper - endpoints only call `decider.decide()`,
  no business logic in the transport layer)
- [x] Add FastAPI endpoints (`POST /decisions`, `GET /health`)
- [x] Add request validation (automatic: `Invoice` as the endpoint's Pydantic parameter type -
  malformed bodies get a 422 for free)
- [x] Connect AI recommendation input (`Decider.decide()` builds `AgentState` and runs
  `build_agent_graph(...)`, reusing the existing agent - not reimplemented)
- [x] Return final router decision (`Decision` returned directly; `AgentError` from the agent
  is caught and falls back to `route_decision(invoice, recommendation=None, ...)` - the router's
  own safe path - documented and tested, never a silent crash)
- [x] Add API documentation (automatic: FastAPI generates OpenAPI/Swagger UI at `/docs` from the
  existing Pydantic models - no extra work needed, satisfies D4)

Also added (not in the original task list, cheap and directly serves M14/M15):
- Structured JSON logging (`services/decision/service/logging_config.py`, stdlib only) with
  `correlation_id` on request/response/fallback log lines.
- `X-Correlation-Id` request header support (used if supplied, generated as a UUID otherwise;
  echoed back in the response header and in `Decision.correlation_id`).
- A global FastAPI exception handler for genuinely unexpected errors (distinct from the
  documented `AgentError` fallback) - logs with correlation_id, returns a JSON 500 instead of
  FastAPI's default undocumented one.

Tests: `tests/integration/test_decision_service.py` (7 tests, `TestClient` + `MockProvider`/a
stub, no real network) - valid invoice -> decision, invalid invoice -> 422, `AgentError` ->
human_review fallback (not a 500), health check, correlation-id echo/generation, and an
end-to-end M12 proof through the real HTTP layer (over-ceiling invoice + an LLM mocked to
confidently recommend approval still comes back `human_review`).

---

# Phase 3 — AI Agent Integration

## Goal

Demonstrate AI Engineering requirements.

The agent provides:

- Extracted invoice information
- Policy reasoning
- Confidence score
- Recommendation

The agent does NOT decide.

## Tasks

- [x] Implement LangGraph agent flow (`services/decision/agent/`: `preprocess` -> `ClassifyNode`
  -> `RouterNode`, wired by `build_agent_graph(provider, thresholds)`; provider and thresholds
  both dependency-injected, not hardcoded/global). `AgentState.raw_llm_response` retains the
  LLM's exact text even on a successful parse, for F9 audit trail purposes. Second-round design
  review incorporated: `policy` deliberately stays in state (not injected as a fixed dependency)
  because N5 (RAG over policy) will need it to vary per-invocation once built.
- [x] Add structured output (Groq strict `json_schema` mode via `LLMProvider.complete(schema=...)`;
  `openai/gpt-oss-120b` constrained decoding guarantees the response parses as `Recommendation` -
  no manual JSON-mode parsing/retries needed). **Numeric bound (min/max on `confidence`) support
  in Groq's strict mode is not yet confirmed against the real API** - `scripts/smoke_test_groq_strict.py`
  is written and ready but could not be run to completion in this environment (SSL/network
  restriction blocks egress to api.groq.com here) - run it manually before calling this
  production-ready. See that script's docstring.
- [x] Add policy context input (`AgentState.policy`, consumed by `preprocess` to build the prompt)
- [x] Add LLM provider abstraction (`services/decision/accessors/`: `LLMProvider` Protocol,
  `GroqProvider`, `MockProvider`, `get_llm_provider()` factory selected by `LLM_PROVIDER` env var)
- [x] Add provider error handling (fail-fast: missing `GROQ_API_KEY` raises `LLMProviderError`
  at construction; SDK errors and empty completions are wrapped, never swallowed; the agent graph
  wraps `LLMProviderError`/schema-parse failures as `AgentError` and never falls back to
  `recommendation=None` silently inside the graph - see `services/decision/agent/nodes.py`
  module docstring for the documented caller-owns-fallback contract)

## Fallback Strategy

If LLM integration becomes risky:

Use a deterministic/mock agent while preserving the architecture:

Agent → Recommendation → Router → Decision

---

# Phase 4 — Core Workflow

## Goal

Create the complete invoice lifecycle.

## Flow

```
Submit Invoice
      |
      v
Intake Service
      |
      v
Decision Service
      |
      +------------+
      |            |
      v            v
Approval      Payment
```

## Tasks

- [x] Create Intake Service (`services/intake/`: `IntakeService`/`build_intake_service`
  transport-agnostic core + thin `app.py` FastAPI wrapper, same pattern as Decision Service)
- [x] Create invoice submission endpoint (`POST /invoices`, 202 Accepted)
- [x] Generate tracking id (UUID, doubles as the `correlation_id` passed to Decision Service -
  `Invoice.id` itself is untouched, stays the caller's own business field)
- [x] Implement asynchronous processing (FastAPI `BackgroundTasks` - explicitly documented as a
  temporary stand-in for Dapr pub/sub, not a production queue: no durability across restarts, no
  retry, no multi-worker scaling; accepted limitation for this phase)
- [x] Persist submission state (`InvoiceRepository` Protocol + `InMemoryInvoiceRepository`, same
  pattern as `LLMProvider`). **Superseded as `create_app()`'s default by `DaprStateInvoiceRepository`
  (Phase 7 step 4, below)** - `InMemoryInvoiceRepository` kept, unchanged, still the default in
  tests (fast, no Dapr needed). A future `PostgresInvoiceRepository` swaps in the same way, without
  touching `IntakeService`.
- [x] Add status endpoint (`GET /invoices/{tracking_id}`, returns a slim `SubmissionStatusResponse`
  - deliberately not the internal `Submission` record, to avoid leaking the dedup key or raw
  internal error text to an external caller)

Also required and added:
- [x] Duplicate detection (F3): `compute_dedup_key` (from `shared.contracts.models`) checked
  against the repository before storing; `is_duplicate` threaded through to Decision Service via
  a new `?is_duplicate=` query param on `POST /decisions` (Decision Service's endpoint didn't
  expose this before - additive change, verified against all pre-existing tests)
- [x] `DecisionServiceClient` Protocol + `HttpDecisionServiceClient` - Intake talks to Decision
  over HTTP, not a direct Python import of `Decider`, so the two remain independently deployable
  (M3). **Superseded as `IntakeService`'s default transport by `DecisionPublisher`/Dapr pub/sub
  (Phase 7 step 3, below)** - turned out to be a structural change, not just a transport swap
  (a fire-and-forget publish can't return a `Decision` the way a synchronous HTTP call could).
  Kept, tested, and still usable standalone (ARCHITECTURE.md §7's "sync invocation only where
  immediate response required", and for local dev without Dapr sidecars).
- [x] `shared/contracts/` extraction (Invoice/Decision/Recommendation/enums/`compute_dedup_key`)
  done first, as a prerequisite - both services depend on one canonical source now instead of
  Intake importing across the service boundary
- Explicitly deferred, with reasons recorded: client-side retry on `HttpDecisionServiceClient`
  (no idempotency-key support on Decision Service yet - naive retry risks double LLM invocation)
  and transport-level idempotency for `POST /invoices` (F3's business-level dedup already
  prevents double-processing; only a client-retry UX gap remains, not a safety one)

---

# Phase 5 — Human Approval Flow

## Goal

Support escalated decisions.

## Tasks

- [x] Create Approval Service (`services/approval/`: `ApprovalService`/`build_approval_service`
  transport-agnostic core + thin `app.py` FastAPI wrapper, same pattern as Intake/Decision)
- [x] Create human review queue (`GET /approvals`, backed by `DaprStateApprovalRepository`'s
  append-only `approval:index` - the only way to enumerate keys in a Dapr state store with no
  Query API. Also added `GET /approvals/{tracking_id}` for a single item, matching the existing
  Intake/Decision `GET /{id}` pattern.)
- [x] Display:
  - Invoice data (`PendingApproval.invoice`)
  - Agent recommendation (`PendingApproval.recommendation` - `None` only when the agent itself
    failed, `AgentError` fallback, always `route=HUMAN_REVIEW` in that case)
  - Confidence score (`recommendation.confidence`, part of the same object)
  - Policy reasons (`PendingApproval.decision.reason` / `.triggered_rules`)

  Required a real, reviewed design decision to reach: the pre-existing `decision.completed` event
  only carried the bare `Decision` (route/reason/triggered_rules/correlation_id), not the invoice
  or the agent's recommendation. Fixed at the source: `Decider.decide()` now returns an internal
  `DecisionOutcome` (decision + recommendation), never crossing a service boundary itself; the
  Decision service's `invoice.submitted` subscription handler builds an enriched
  `DecisionCompletedEvent` (invoice + decision + recommendation) from it and publishes that.
  `POST /decisions`'s external HTTP response is unchanged (still bare `Decision` - D4 API
  stability; proven by a dedicated regression test asserting the response's JSON keys never
  include `recommendation`).
- [x] Implement approve action (`POST /approvals/{tracking_id}/approve` - PENDING/WAITING_INFO ->
  APPROVED, publishes `approval.completed` with `resolution=approved`)
- [x] Implement reject action (same, `resolution=rejected`)
- [x] Implement request-information action (`POST /approvals/{tracking_id}/request-info` ->
  WAITING_INFO, publishes nothing - per ADR-003, once escalated the human owns the decision.
  Documented gap: there is no Gateway/UI route yet for the submitter to actually supply more
  info and trigger a resume - the queue correctly keeps showing the item as WAITING_INFO
  [non-terminal], but nothing currently moves it back out of that state automatically)
- [x] Implement durable pause/resume (M11) - `DaprStateApprovalRepository`, same pattern as
  `DaprStateInvoiceRepository`: `approval:{tracking_id}` -> full `PendingApproval` JSON,
  `approval:index` -> append-only tracking_id list, both written in one atomic
  `execute_state_transaction`. Verified for real: force-recreating the `approval`
  container + its Dapr sidecar mid-flow, a PENDING-turned-APPROVED item and a WAITING_INFO item
  both survived and remained fully actionable afterward (approved the WAITING_INFO item
  successfully post-restart) - see "Verification (Approval Service, Phase 5)" below.

Also fixed, a real bug found during design review before any code was written: idempotent
handling of `decision.completed` in `handle_decision_completed()`. The original design would have
let Dapr's at-least-once redelivery of that event - arriving *after* a human already
approved/rejected the item - silently revert the status back to PENDING. Fixed to mirror
`IntakeService.complete()`'s existing pattern: if a record already exists for the tracking_id (in
any status), it's a no-op; a new `PendingApproval` is only ever created on first sighting.
Proven by a dedicated regression test (`test_handle_decision_completed_is_idempotent_after_approval`).

## Verification (Approval Service, Phase 5)

Ran for real, not just described:
- Full TDD cycle throughout (RED confirmed before every GREEN) - 223 tests total (32 new for
  Approval, plus updated Decision tests for the `DecisionOutcome`/`DecisionCompletedEvent`
  enrichment), ruff, and mypy all pass (the two pre-existing mypy findings in
  `scripts/run_agent.py` and `tests/unit/shared/test_models.py` predate this phase, confirmed via
  `git stash` against the clean branch - not introduced here).
- `docker compose up --build -d` - all **9** containers (previous 7 + `approval` + `approval-dapr`)
  came up healthy.
- Submitted INV-1003 (client dinner, $1820, over ceiling + missing client name - `expected.route:
  human_review`) via `POST /invoices` - confirmed it reached `GET /approvals` with the full F4
  payload (invoice, recommendation with confidence 0.95, decision reason/triggered_rules).
- Submitted INV-1001 (auto_approve fixture) - confirmed it never reaches `GET /approvals` at all
  (Approval correctly ignores non-`human_review` routes, choreography).
- `POST /approvals/{id}/approve` - status became `approved`, confirmed via `GET
  /approvals/{id}` and via `docker compose logs approval` (`approval_resolved` logged). Retried
  the same approve - got `409`, and the publish count did not increase (idempotency guard against
  double-click/retry proven live, not just in a test).
- Submitted a second INV-1003-style item, called `POST /approvals/{id}/request-info` - status
  became `waiting_info`, and it still appeared in `GET /approvals` (non-terminal, as designed).
- **The M11 restart-survival test** (the central proof of durable HITL):
  `docker compose up -d --force-recreate approval approval-dapr` (not `restart` alone - same
  operational lesson learned in the Dapr-state-for-Intake phase) while one item was `approved` and
  another was `waiting_info`. Both containers came back healthy; `GET /approvals` afterward showed
  both items with their pre-restart statuses intact. Then successfully called
  `POST /approvals/{id}/approve` on the surviving `waiting_info` item, confirming the service was
  fully functional after the restart, not just serving stale reads.

---

# Phase 6 — Payment Flow

## Goal

Demonstrate reliable transaction handling.

Required scenario:

INV-1012 — payment failure + compensation.

## Tasks

- [x] Create Payment Service (`services/payment/`: `PaymentService`/`build_payment_service`
  transport-agnostic core + thin `app.py` FastAPI wrapper, same pattern as Intake/Decision/Approval.
  Subscribes to `decision.completed` [route=auto_approve] and `approval.completed`
  [resolution=approved], both converging on the same saga)
- [x] Implement payment reservation (`BudgetRepository.reserve()` - atomic check-and-deduct via
  Dapr state's ETag optimistic concurrency, confirmed against the installed `dapr==1.18.1` SDK
  source before writing any code: `TransactionalStateOperation` accepts `etag=` directly, and an
  ETag conflict inside `execute_state_transaction` surfaces as `DaprGrpcError` - a `grpc.RpcError`
  subclass, matching the `except grpc.RpcError` idiom already used everywhere else. Deliberately
  never uses `save_state()` directly for the conditional write - its ETag-conflict path raises
  `DaprInternalError`, NOT a `grpc.RpcError` subclass, which would silently break that idiom)
- [x] Implement failure simulation (`PaymentGateway` Protocol, mirroring `LLMProvider`/`MockProvider`
  exactly - `FakePaymentGateway` for tests, `SimulatedPaymentGateway` as the actual production
  default since there is no real payment processor in this project. Deterministic, not random:
  fails only for invoice ids listed in `PAYMENT_SIMULATE_FAILURE_IDS` [set to `INV-1012` in
  `docker-compose.yml`, so the journey is demonstrable with a plain `docker compose up`])
- [x] Implement compensation action (`BudgetRepository.release()`, crediting back exactly the
  `reserved_amount` stored on the `PaymentRecord` at reservation time - never recomputed from
  `invoice.total`, so the two can never drift. Guarded against double-release/corruption: raises
  `BudgetCorruptionError` if the credit would push `remaining` above `total`)
- [x] Document Saga flow (ADR-004 orchestration style, Payment is the sole coordinator; see
  ARCHITECTURE.md §9 Saga & Compensation, updated this phase, and the state machine in
  `services/payment/models.py`)

Also required and added, beyond the literal task list:
- **Crash-recovery state machine** (not just event-redelivery idempotency): `PaymentRecord` has a
  `RESERVED` status between reserve and charge - if the process crashes there, a redelivery of the
  same triggering event resumes exactly at the charge step, never re-reserving. Two dedicated unit
  tests (success and failure resume paths) prove this, marked MUST-HAVE/blocking for Definition-of-
  Done per review. `COMPLETED`/`FAILED` are terminal; redelivery after either is a no-op (mirrors
  `ApprovalService.handle_decision_completed`'s exact idempotency fix).
- **Business failure vs. infra failure, deliberately separated**: `InsufficientBudgetError`/
  `BudgetNotFoundError` are caught inside `PaymentService` and become a terminal `FAILED`
  `PaymentRecord`. `BudgetRepositoryError` (genuine Dapr/infra failure, raised only after ETag-
  conflict retries are exhausted) is deliberately never caught there - it propagates uncaught out
  of the subscription handler, causing Dapr to redeliver the event later once the infra issue
  clears, instead of silently recording a fake business failure. A global exception handler
  (mirroring Decision's) logs these with `correlation_id` instead of a bare traceback.
- **Topic consolidation, a third time**: `payment.completed`/`payment.failed` collapsed into a
  single `payment.completed` topic carrying a `resolution` field - the same consolidation already
  applied twice to `decision.completed` and `approval.completed`, reviewed and confirmed rather than
  assumed.
- **Budget seeding from external config**, not hardcoded (CLAUDE.md: never hardcode policy values) -
  `policy/budgets.json` + `services/payment/budgets_loader.py` (mirrors
  `services/decision/service/policy_loader.py`), loaded once at `create_app()`'s lifespan startup,
  calling `ensure_seeded()` per department - never overwrites an in-progress budget on restart.
- **Storage decision, reviewed and resolved**: ARCHITECTURE.md §8 said Payments/Budgets live in
  PostgreSQL, but §9's Budget Concurrency mechanism explicitly required Dapr state with ETag - a
  real contradiction, resolved in favor of Dapr state for this phase (matches §9 exactly, reuses
  100% proven patterns, matches every prior service's InMemory→DaprState→Postgres migration path;
  Postgres remains deferred/unused, exactly as already documented). §8 updated accordingly.
- **Real, load-bearing finding**: Payment is the first service with genuine async startup work
  (budget seeding via a FastAPI lifespan hook). Confirmed by reading the installed
  `starlette==1.3.1` source that `TestClient.__enter__` is the only place that runs ASGI lifespan
  startup - a bare `TestClient(app)` without `with`, the pattern every other integration test file
  in this repo uses, never runs it. `tests/integration/test_payment_service.py` uses
  `with TestClient(app) as client:` throughout, with a dedicated regression test locking in the
  gotcha (proves the bare-`TestClient` pattern does NOT seed budgets), not just documenting it in
  prose.

## Verification (Payment Service, Phase 6)

Ran for real, not just described:
- Full TDD cycle throughout (RED confirmed before every GREEN) - 317 tests total (94 new for
  Payment), ruff, and mypy all pass.
- `docker compose up --build -d` - all **11** containers (previous 9 + `payment` + `payment-dapr`)
  came up healthy; confirmed `GET /budgets/{department}` correctly seeded from
  `policy/budgets.json` at startup for all three configured departments.
- **INV-1012 (payment failure + compensation)**: submitted the real fixture, confirmed it escalated
  (HW-02, over ceiling), approved it, and confirmed via `docker compose logs payment` the exact
  expected sequence - `budget_reserved` → `payment_failed_compensated` → `payment_event_published`,
  with no `payment_completed` line. `GET /payments/{id}` showed `status: failed` with the simulated
  decline reason; `GET /budgets/engineering-2026Q2` showed `remaining` back to its exact pre-
  submission baseline - no orphaned reservation.
- **Happy path control**: submitted an auto-approve invoice (no human review needed), confirmed
  `status: completed` and the budget correctly deducted by exactly the invoice total - proves the
  success path works over the real stack, not just the failure path.
- **Restart survival (M11-equivalent for Payment)**: approved a second hardware invoice, then
  `docker compose up -d --force-recreate payment payment-dapr` after it reached `COMPLETED`.
  Confirmed both the `PaymentRecord` and the department budget's `remaining` survived the restart
  unchanged, and specifically that `ensure_seeded()`'s startup call did NOT reset the already-
  progressed budget back to its seed value - proving the "only seed if absent" guard works for
  real, not just in a unit test. (Catching the `RESERVED` transient status live was not attempted -
  the saga completes in milliseconds with no artificial delay, making it impractical to hit that
  window manually; the crash-recovery-from-RESERVED logic itself is rigorously proven by the two
  dedicated deterministic unit tests instead.)
- **INV-1014A/B (budget concurrency) - required, not optional, run for multiple iterations**:
  `python scripts/verify_inv1014_concurrency.py 3` - a real script using `httpx.AsyncClient` +
  `asyncio.gather` so the two `approve` calls genuinely overlap in time (not sequential curl calls
  that could "pass" by timing luck). Refined after review: the script resets `marketing-2026Q2`'s
  budget to a fresh $1000 before every iteration (via `docker exec redis-cli HSET` directly on
  Dapr's Redis-stored state - verified empirically first, not assumed, that Dapr stores state as a
  `{app-id}||{key}` Redis HASH with `data`/`version` fields, `version` doubling as the ETag;
  resetting only `data` leaves Dapr's own ETag bookkeeping untouched). Deliberately never exposed
  as an HTTP endpoint - a "reset budget" API in a service that manages money would be a real
  operational hazard, so this reset only ever happens by reaching directly into the test
  infrastructure's own Redis, external to the app. With the reset in place, **all 3 iterations**
  (not just the first) showed exactly one invoice `completed` and one `failed` (insufficient
  budget), `remaining` landing at exactly $400 every time - matching the fixture's own expected
  math on every run, not just once. Confirmed via `docker compose logs payment` that exactly one
  `budget_reserved`+`payment_completed` pair occurred per iteration, the loser always
  `payment_rejected_insufficient_budget` - never both succeeding, never a negative balance, across
  every iteration.

---

# Bugfix — Intake's decision.completed parsing (predates Phase 6.5)

## Root cause

Found live, not guessed, while verifying the Intake duplicate-publish fix above: submitting
a fresh (non-duplicate) invoice, Decision correctly auto-approved it and published
`decision.completed`, but Intake's own subscription handler
(`services/intake/app.py::handle_decision_completed`) crashed with a `pydantic.ValidationError`.
Timeline (`git log -p`): the handler's `Decision.model_validate(body["data"])` was written in
commit `e4aed1e` (M5, Intake↔Decision pub/sub) expecting a bare `Decision`. Commit `7e7dfeb`
("Add Approval Service... with decision.completed enrichment") later changed Decision's real
publish format to the enriched `DecisionCompletedEvent` (invoice + decision + recommendation),
since Approval needs all three for F4 - but Intake's own consumer of that same topic was never
updated to match. Approval/Payment/Notification's handlers were all written against (or
updated to) the enriched shape correctly; only Intake's was left behind.

## Impact

`GET /invoices/{tracking_id}` stayed stuck on `"processing"` forever for any invoice that goes
through Decision for real (auto_approve/reject/human_review) - Dapr redelivers the failed
event indefinitely, always hitting the same crash. The actual business flow (payment,
approval, notification) was unaffected - each of those services has its own separate Dapr
subscription with correct parsing, so this was purely Intake's own status-tracking silently
lying, not a break in any F/M requirement. Went unnoticed because the existing integration
test's simulated payload (`_post_decision_completed`) sent a bare `Decision`, not the real
enriched shape Decision actually publishes - a gap between what was tested and what production
actually sends.

## Fix

`services/intake/app.py::handle_decision_completed` now parses `DecisionCompletedEvent.model_validate(body["data"])`
(identical to `services/notification/app.py`'s already-correct handler) and calls
`service.complete(event.decision.correlation_id, event.decision)`. `IntakeService.complete()`
itself is unchanged - it always took a bare `Decision` correctly; only the transport-layer
parsing needed fixing. Deliberately no backward-compatibility parsing for the old bare-`Decision`
shape and no new try/except around the parse call - see the plan file's reasoning: no
replica-set/rolling-deploy in this docker-compose setup makes the old shape unreachable in
practice, and every other subscription handler in this project already lets parse failures
propagate uncaught (loud 500 + traceback, Dapr retries) rather than swallowing them quietly -
consistency with that established, deliberate pattern over defensive coding for a scenario
that can't happen.

## Tests

`tests/integration/test_intake_service.py`'s `_post_decision_completed()` rebuilt to construct
the real `DecisionCompletedEvent` via the shared Pydantic model (not a hand-rolled dict) -
a contract regression test by construction: any future drift between the real event shape and
what's tested fails automatically. New test
`test_decision_completed_event_with_recommendation_is_parsed_correctly` reproduces the exact
crashing payload shape (a non-`None` `recommendation`, the field that triggered
`extra_forbidden`). RED confirmed first: ran the updated tests before touching `app.py` and saw
the identical `ValidationError` seen live, proving the test genuinely catches the bug.

## Lessons learned

Event schema changes must update every subscriber **and its integration tests** together, not
just the subscribers exercised by the phase making the change. **TODO, not implemented now**:
consider a shared `tests/support/` fixture for building
`DecisionCompletedEvent`/`ApprovalCompletedEvent`/`PaymentCompletedEvent` test payloads -
today each service's test file (`test_service.py` in Payment, Notification, and now Intake)
builds its own local, ad-hoc helper, which is exactly the kind of duplication that let this
particular drift go unnoticed.

## Verification (live, docker compose)

Ran for real, not just described - required a full rebuild of `intake` (shares a Dockerfile
with `decision`, so both got recreated). **Lesson from this session's own experience**:
`docker compose up --build -d intake` alone left `intake-dapr`'s network namespace pointing
at the old (pre-rebuild) container, breaking its Redis DNS resolution (`lookup redis` errors)
- fixed with `docker compose up -d --force-recreate intake-dapr decision-dapr` immediately
after; `restart` is not sufficient, only `--force-recreate` reattaches the sidecar to the
current container.
- **Non-duplicate, auto-approve** (fresh invoice): `GET /invoices/{id}` reached
  `"status": "completed"` (previously stuck forever on `"processing"`) - direct proof the
  parsing bug is fixed.
- **INV-1007-equivalent (fresh duplicate pair)**: Notification received the push
  (`{"notified": true}`, log shows `source=decision.completed`); confirmed Payment's logs
  show zero activity for the duplicate's tracking_id (F3 preserved live, not just unit-tested).
- **INV-1003** (escalate -> approve -> pay): full journey re-run end-to-end; Payment reached
  `"status": "completed"`, Notification `{"notified": true}`, and - the actual regression
  target - Intake's own `GET /invoices/{id}` correctly reached `"completed"` instead of
  staying stuck.
- **INV-1012** (escalate -> approve -> simulated payment decline -> compensation): Payment
  reached `"status": "failed"` with the simulated decline reason, Notification
  `{"notified": true}`, Intake `"completed"`.
- **Redelivery idempotency**: re-POSTed the identical `payment.completed` event body to
  Notification's subscription route a second time (simulating Dapr redelivery) - logged
  `notification_already_sent_skipping`, confirmed only one `notification_delivered` line
  total across both attempts.
- Full local suite (367 tests), `ruff check .`, `mypy .` all pass after the live run.

---

# Phase 6.5 — Notification Service

## Goal

Close the last gap in the choreography chain: every terminal outcome (auto-approved +
paid, human-approved + paid, human-rejected, payment-failed, router-rejected, duplicate)
reaches the submitter via a push notification, not just pull (`GET /invoices/{id}`).

Required scenarios: all four already-shipped fixture journeys (INV-1003, INV-1007,
INV-1012, INV-1015) each produce exactly one notification.

## Tasks

- [x] Create Notification Service (`services/notification/`: `NotificationService`/
  `build_notification_service` transport-agnostic core + thin `app.py` FastAPI wrapper,
  same pattern as Approval/Payment). Subscribes to three topics: `decision.completed`
  [route in {reject, duplicate}], `approval.completed` [resolution=rejected],
  `payment.completed` [resolution in {completed, failed} - unconditional, both act], each
  converging on a shared `_notify()` helper.
- [x] Implement minimal idempotency guard (`NotificationRepository` Protocol +
  `InMemoryNotificationRepository` + `DaprStateNotificationRepository`) - point
  lookup/point write only, deliberately no append-only index (nothing needs to enumerate
  notified tracking_ids - no `GET /notifications` listing endpoint required by any doc,
  unlike Approval's `list_pending()`/Payment's `list_all()`). `mark_notified()` writes via
  `execute_state_transaction` with a single `etag=None` operation, deliberately not
  `save_state()` - exception-handling consistency, not atomicity (same reasoning already
  established in the Payment phase: `save_state()`'s ETag-failure path is
  `DaprInternalError`, not a `grpc.RpcError` subclass; `execute_state_transaction`'s is
  `DaprGrpcError`, matching the `except grpc.RpcError` idiom used everywhere else).
- [x] Implement notification channel (`NotificationChannel` Protocol, mirroring
  `PaymentGateway` exactly; `LoggingNotificationChannel` as the real, only production
  implementation - there is no real email/SMS/webhook backend in this project, so
  structured logging IS production here, the same posture as Payment having no real
  payment processor. Named "Logging", not "Simulated" [Payment's specific choice] - it
  never manufactures a synthetic failure; logging genuinely is the delivery mechanism.
  `FakeNotificationChannel` for tests, supports injected failures via `fail_for` and
  `fail_first_n_calls`).
- [x] Business/infra failure separation (mirrors Payment): `channel.send()` failures are
  never caught inside `NotificationService` - they propagate uncaught out of the
  subscription handler (non-2xx, Dapr redelivers later). `send()` happens BEFORE
  `mark_notified()` - if `send()` fails, no mark is written, so redelivery retries the
  actual send; if `send()` succeeds but the process crashes before the mark persists, a
  redelivery sends a harmless duplicate (safer than the alternative). Proven not just by
  a "failure doesn't corrupt state" test but by a dedicated
  `test_redelivery_after_failed_send_succeeds_and_marks_notified` showing the retry
  actually recovers.
- [x] Updated `ARCHITECTURE.md` §3 (added the missing `DEC -> NOT` edge), §4 (Notification's
  DB column + a terminal-consumer lifecycle sentence), §7 (new Decision -> Notification
  communication row + three-topic filter explanation, mirroring the style of the two
  consolidations already documented there), and §8 (new Notification row in the Data
  Architecture table - previously absent, correctly implying no state; corrected the same
  way the Payment phase corrected that table twice).
- [x] Added `notification`/`notification-dapr` to `docker-compose.yml` (port 8004, **with**
  a host port mapping like every other service - an earlier planning draft incorrectly
  assumed Approval/Payment had none; verified directly against the file before writing
  this) and `notification` to both `dapr/components/{pubsub,statestore}.yaml` scopes.

Also required and added, beyond the literal task list:
- **A known race, documented as an accepted non-goal (same category as Payment's
  reserve/save race)**: `_notify()`'s three steps (`already_notified` -> `send` ->
  `mark_notified`) are not atomic together - two near-simultaneous redeliveries of the
  same event could both observe `already_notified=False` before either sends, causing a
  duplicate (harmless) notification. Not fixed here, documented explicitly.
- **A real finding about the JSON log formatter**: `JsonFormatter` (duplicated across
  every service) only ever promotes `record.correlation_id` out of `extra=` - every other
  key passed via `extra=` is silently dropped from the emitted JSON line. This is harmless
  for the other services (their extras are decoration), but load-bearing for Notification,
  whose entire live-verification plan depends on `docker compose logs notification`
  showing the human-readable message. `LoggingNotificationChannel.send()` therefore
  interpolates the tracking id, invoice id, submitter, source topic, and message directly
  into the primary log message string (`%s`-style), not solely via `extra=`.
- **A genuine, documented gap in `ApprovalCompletedEvent`**: `decision.reason` is the
  *original escalation reason* (why an item went to human review), not the approver's own
  rationale for rejecting it - `ApprovalService.reject()` never collects a separate
  rejection reason. The notification text for that path (`"Rejected by approver: ..."`)
  uses the best available text, not a perfect fit - documented as a TODO in
  `services/notification/service.py` itself, not just here, so it isn't rediscovered as a
  mystery later. Closing it would need a new `reason` parameter on `reject()`, out of
  scope for this phase.
- **`GET /notifications/{tracking_id}` debug/ops endpoint** - always returns `200` (never
  `404`; "not yet notified" is a valid state, not an error, unlike Payment's
  `GET /payments/{id}`), documented explicitly as an ops/debug endpoint (not part of the
  public API contract) and as "not guaranteed to be retained forever" (so a future
  TTL/deletion policy on the underlying Dapr key stays semantically correct).
- **A real, pre-existing gap found during live verification, fixed as part of this phase**:
  submitting INV-1007 (duplicate) live produced **no** notification at all -
  `GET /notifications/{tracking_id}` returned `{"notified": false}`. Root-caused (not
  guessed) by reading `IntakeService.process()`: a known duplicate is short-circuited
  before `invoice.submitted` is ever published, so Decision never sees it and
  `decision.completed` never fires - a gap that predates this phase, just never exposed
  because nothing consumed `decision.completed` for a route Decision never emits. Fixed by
  having Intake publish `decision.completed` itself for this one case (new
  `services/intake/decision_completed_publisher.py`, mirroring
  `services/decision/service/outcome_publisher.py` exactly), guarded by the same
  idempotency check Intake already uses elsewhere (`status == COMPLETED` before
  publishing). Two things proven explicitly, not assumed: (1) Payment still ignores
  `route == duplicate` - `test_handle_decision_completed_ignores_non_auto_approve_routes`
  parametrized over `{HUMAN_REVIEW, REJECT, DUPLICATE}` (F3 preserved); (2) Intake's own
  subscription to `decision.completed` receiving its own published event back
  (self-loopback, since Intake publishes to the same topic it consumes) is a safe no-op -
  `test_complete_is_idempotent_when_intakes_own_published_event_loops_back`.

## Verification (Notification Service, Phase 6.5)

Ran for real, not just described:
- Full TDD cycle throughout (RED confirmed before every GREEN) - ruff and mypy pass.
- `docker compose up --build -d` - all 13 containers (previous 11 + `notification` +
  `notification-dapr`) came up healthy.
- **INV-1003** (escalate + approve + pay): re-verified the existing journey, confirming
  `docker compose logs notification` shows exactly one `notification_delivered` line
  (`source=payment.completed`) once the payment saga completes, and
  `GET http://localhost:8004/notifications/{tracking_id}` returns `{"notified": true}`.
- **INV-1012** (payment failure + compensation): re-verified, confirming exactly one
  notification fires from `payment.completed` [resolution=failed], with the simulated
  gateway decline reason visible in the log message text.
- **INV-1007** (duplicate): **initially failed live** - see the gap documented above.
  Re-verified successfully after both the Intake duplicate-publish fix and the
  decision.completed parsing bugfix landed (below): a fresh duplicate pair produced
  `GET http://localhost:8004/notifications/{tracking_id}` -> `{"notified": true}`, with
  `docker compose logs notification` showing exactly one `notification_delivered` line
  (`source=decision.completed`, "Invoice identified as duplicate: ..."), and confirmed live
  that Payment never touched the duplicate's tracking_id (F3).
- **INV-1015** (reject, alcohol-only): newly verified - a `decision.completed`
  [route=reject] event produces exactly one notification, with the MEAL-03 reason text
  visible.
- **Idempotency, live**: re-POSTing the identical event body a second time directly to a
  notification subscription route (simulating Dapr redelivery) produces **no** second
  `notification_delivered` log line, and `GET /notifications/{tracking_id}` is unchanged.
- Full local suite re-run after the live run, confirming no regression in the other 4
  services.

## Verification (Intake duplicate-publish fix)

- Full TDD cycle on `tests/unit/intake/test_service.py` and
  `tests/integration/test_intake_service.py` (RED confirmed before GREEN) - existing tests
  renamed for clarity where they now describe `invoice.submitted`-specific behavior
  (`test_process_does_not_publish_invoice_submitted_for_known_duplicate`), new tests added
  for the new publisher, the double-publish idempotency guard, the self-loopback
  no-op, and publish-failure handling (logs, does not revert `COMPLETED` to `FAILED`).
  `tests/unit/payment/test_service.py`'s route-filter test parametrized over
  `{HUMAN_REVIEW, REJECT, DUPLICATE}` to prove F3 explicitly.
- `python -m pytest`, `ruff check .`, `mypy .` - all pass (see full-suite verification below).
- Live `docker compose` re-verification of INV-1007 with the fix in place: **done**, see
  above - required first fixing the decision.completed parsing bugfix (below), since
  Notification's own live check depends on Intake correctly reaching a terminal status.

---

# Phase 7 — Infrastructure

## Goal

Make the complete system runnable.

## Tasks

- [x] Create Docker Compose environment (`docker-compose.yml`, repo root)
- [x] Containerize services (single shared `Dockerfile` for Intake + Decision - both share
  `pyproject.toml`/`shared/`, differ only in the `command:` uvicorn target/port; not two
  near-identical Dockerfiles to maintain - M3 is about containers being separate, not about
  Dockerfiles being separate, and the two services do run as two separate containers)
- [x] Add PostgreSQL (`postgres:16-alpine`, no volumes yet - reserved for Dapr state store,
  a later phase; deliberately not behind `profiles:` - M4 requires the whole system, including
  not-yet-used infrastructure, to come up with one plain `docker compose up`)
- [x] Add Redis (`redis:7-alpine`, same reasoning as Postgres above - reserved for Dapr pub/sub)
- [x] Configure Dapr - step 1, infra only (M5): `daprd` sidecar per service (`intake-dapr`,
  `decision-dapr`) + `placement` control-plane container, `dapr/components/{pubsub,statestore}.yaml`
  backed by Redis. No service code changed yet - Intake and Decision still talk HTTP-direct, as
  before. See "Verification (Dapr step 1)" below.
- [x] Configure Pub/Sub communication - step 3: `IntakeService` publishes `invoice.submitted`
  via `DecisionPublisher`/`DaprDecisionPublisher` (Dapr Python SDK, `dapr.aio.clients.DaprClient`);
  Decision subscribes (`dapr-ext-fastapi`'s `DaprApp`), runs the **unmodified** `Decider`, and
  publishes the result to `decision.completed` via `DecisionOutcomePublisher`; Intake subscribes
  and calls `IntakeService.complete()`. Verified end to end over the real Docker network, not just
  `TestClient` - see "Verification (Dapr pub/sub, step 3)" below.
- [x] Configure Dapr state (idempotency/dedup) - `DaprStateInvoiceRepository` replaces
  `InMemoryInvoiceRepository` as Intake's default `InvoiceRepository`, backed by the `statestore`
  component. See "Verification (Dapr state, step 4)" below. HITL pause-resume was the separate
  future step below (now done, Phase 5).
- [ ] Configure Dapr secrets

## Verification (done)

Found and fixed two real bugs during planning/implementation, before ever running Docker:
- `[tool.setuptools] packages = ["services", "shared"]` was an explicit, incomplete list that
  does not auto-discover subpackages (`services.decision.router`, etc.) - never exercised
  because all 128 tests ran via pytest's `pythonpath = ["."]`, never via `pip install .`. Fixed
  with `[tool.setuptools.packages.find]` + `include = ["services*", "shared*"]`, plus
  `ENV PYTHONPATH=/app` in the Dockerfile as a defensive backstop (same mechanism already
  proven by pytest).
- Original `Dockerfile` draft did `COPY pyproject.toml ./` then immediately `RUN pip install .`,
  before `services/`/`shared/` existed in the build context - setuptools would have had nothing
  to discover regardless of the `packages.find` fix. Fixed by moving all `COPY` commands before
  `RUN pip install .`, trading away a dependencies-only cache layer for build-order correctness.

Ran for real (not just described):
- `docker compose up --build -d` - both images built, all four containers (`intake`, `decision`,
  `postgres`, `redis`) came up; `intake` correctly waited for `decision`'s healthcheck
  (`depends_on: condition: service_healthy`) before starting.
- `python scripts/smoke_test_compose.py` - passed: Intake received the invoice, called Decision
  over the Docker network (`http://decision:8001`, not localhost), got back a completed decision
  with a matching `correlation_id`. Proves service discovery, `POST /invoices` -> `BackgroundTasks`
  processing -> `HttpDecisionServiceClient` -> Decision's `/decisions` all work end to end inside
  containers, not just under `TestClient`.
- Structured JSON logs on both services confirmed `correlation_id` propagation end to end
  (M14), visible via `docker compose logs`.
- Full local suite re-run after the `pyproject.toml` packaging fix: 128/128 tests, ruff, and
  mypy all still pass.

Known, documented limitation (not a code bug): the actual Groq API call failed inside the
`decision` container during this smoke test run - confirmed via a direct `httpx.get("https://api.groq.com")`
from inside the container, same `CERTIFICATE_VERIFY_FAILED: self-signed certificate in
certificate chain` seen earlier from the host when running `scripts/smoke_test_groq_strict.py`.
This is this sandbox's SSL-intercepting network egress, not something Docker networking fixed
or introduced. The Decision Service's fail-clean fallback (`AgentError` -> `route_decision`
with `recommendation=None`) handled it correctly and returned `human_review` - the smoke test
still passed on its own terms (it deliberately does not assert a specific route, only that the
pipeline completes end to end - see the script's docstring), but this run did not prove the real
LLM call path works through Docker. That still needs `scripts/smoke_test_groq_strict.py` (or this
smoke test) run from a network without SSL interception before Phase 3 is "production-ready".

## Verification (Dapr step 1)

Design decisions made and verified for real, not just described:
- **Redis, not Postgres, backs the Dapr `statestore` component** - `ARCHITECTURE.md` §6/§8
  explicitly separates "Redis = Dapr state store + pub/sub backend" from "PostgreSQL = business
  data (invoices/decisions/payments/budgets), accessed directly, not via Dapr". Both
  `dapr/components/pubsub.yaml` and `statestore.yaml` point at `redis:6379`.
- **`placement` included, `scheduler` deliberately not** - both are Dapr control-plane services
  that exist for actors/Workflow/Jobs API, neither of which this project uses anywhere (durable
  pause/resume for M11 is Dapr *state*, not actors; the agent is LangGraph, not Dapr Workflow).
  `placement` was kept (cheap, no volume, no root user, matches the original ask). `scheduler` was
  cut - it needs a persistent etcd volume and a root user, the one real risk/complexity item in
  this step, for a capability with no planned use. Confirmed empirically: omitting it produces
  only a one-line benign warning (`"No scheduler host addresses provided. Scheduler disabled"`),
  not a failure - `daprd` still reports `"dapr initialized. Status: Running."` on both sidecars.
- **`scopes: [intake, decision]`** on both component YAMLs - Dapr-enforced least-privilege, not
  just documentation; will need extending when Approval/Payment get their own sidecars.

Ran for real:
- `docker compose up --build -d` - all **7** containers (`intake`, `decision`, `postgres`,
  `redis`, `placement`, `intake-dapr`, `decision-dapr`) came up `Up`/healthy; `intake`/`decision`
  healthchecks unaffected by their sidecars (`network_mode: "service:<app>"` shares only the
  network namespace, not the app container's process/filesystem).
- `docker compose logs intake-dapr decision-dapr placement` - both sidecars logged
  `Component loaded: pubsub (pubsub.redis/v1)` and `Component loaded: statestore (state.redis/v1)`
  with no errors, and `placement` logged both `intake` and `decision` connecting successfully.
- `GET http://localhost:3500/v1.0/healthz` from inside both app containers (via `python -c
  urllib.request`, no `curl` in the slim images) - `204` from both.
- `GET http://localhost:3500/v1.0/metadata` from inside both app containers - both list
  `['pubsub', 'statestore']` under `components`, proving the components are actually registered,
  not just "loaded without a log error".
- `python scripts/smoke_test_compose.py` - still passes unchanged: proves the sidecars
  (`network_mode: service:<app>`) didn't disturb the existing direct-HTTP Intake -> Decision flow.
- Full local suite re-run: 128/128 tests, ruff, and mypy all still pass (no service code touched
  in this step - infra-only, by design).

## Verification (Dapr pub/sub, step 3)

Design decisions made and verified for real, not just described (three rounds of review,
each one genuinely reconsidered rather than rubber-stamped):
- **Single topic `decision.completed`, not one per route.** Original plan used 4 topics
  (`decision.approved/escalated/rejected/duplicate`) matching `ARCHITECTURE.md`'s then-literal
  text. Reversed after confirming Dapr supports content-based routing (CEL match rules) for
  subscribers that want to filter - so per-outcome filtering is a subscriber concern, not a
  reason to fragment the publisher's topic space. `ARCHITECTURE.md` §7 updated to match.
- **Official Dapr Python SDK (`dapr`, `dapr-ext-fastapi==1.18.1`, pinned to match the runtime),
  not raw HTTP to the sidecar** - but a real finding corrected the reasoning along the way:
  `dapr-ext-fastapi`'s `subscribe` decorator does **not** auto-unwrap CloudEvents (verified by
  reading its installed source, `dapr/ext/fastapi/app.py`) - it only auto-registers `/dapr/subscribe`
  and the route path. `body["data"]` is still unwrapped by hand in both subscription handlers,
  exactly as raw HTTP would have required.
- **`DaprClient()` is not lazy - confirmed empirically, not assumed.** Its constructor calls
  `DaprHealth.wait_for_sidecar()` synchronously (verified by reading the installed SDK source,
  `dapr/clients/health.py`), blocking up to `DAPR_HEALTH_TIMEOUT` (60s default) retrying a sidecar
  health check. Building it eagerly in `DaprDecisionPublisher.__init__`/`DaprDecisionOutcomePublisher.__init__`
  would have hung `create_app()`'s default wiring (and so plain `pytest`/local dev without a
  sidecar) for up to a minute. Both classes construct the real client lazily, on first `publish()`
  call, and reuse it after that - verified with a dedicated test asserting construction completes
  in under 1 second with no sidecar present.
- **Known duplicates short-circuit in Intake before publishing, never reaching Decision.**
  Verified by reading `route_decision`'s gate 1: it returns immediately on `is_duplicate=True`
  without ever touching the agent's `Recommendation` - so running the LLM for a known duplicate
  was pure waste (confirmed, not assumed), not an audit-trail trade-off. `build_duplicate_decision()`
  (new, in `shared/contracts/models.py`) is the single canonical source both `IntakeService.process()`
  and the router's gate 1 use, so they can't drift apart. Consequence: `InvoiceSubmittedEvent` has
  no `is_duplicate` field - Intake never publishes one that's `True`.
- **`HttpDecisionServiceClient`/`DecisionServiceClient`** kept, unchanged, but no longer wired as
  `IntakeService`'s default - its synchronous `decide() -> Decision` return can't express
  fire-and-forget pub/sub. Test coverage backfilled (`tests/unit/intake/test_decision_client.py`)
  since the integration test that used to exercise it was rewritten around the new transport.

Ran for real:
- Full TDD cycle throughout (RED confirmed before every GREEN) - 160 tests total, ruff, and mypy
  all pass.
- `docker compose up --build -d` - all 7 containers healthy; both sidecars logged clean startup.
- Submitted a real invoice via `curl POST http://localhost:8000/invoices` and polled
  `GET /invoices/{id}` to `completed` - confirmed via `docker compose logs` that the entire path
  is now event-driven with **zero direct HTTP calls between intake and decision**: `intake` logs
  `invoice_received` -> `decision` logs `POST /events/invoice-submitted 200 OK` and
  `decision_requested`/`decision_completed` -> `intake` logs `POST /events/decision-completed 200 OK`
  and `processing_completed`, with the same `correlation_id` end to end (F9).
- Submitted the same vendor/invoiceNumber/total again - completed immediately with
  `route: duplicate`, and `decision`'s logs show no new `/events/invoice-submitted` call at all -
  confirms the short-circuit works for real, not just in unit tests.
- The agent fell back to `human_review` via the same known sandbox SSL restriction documented
  since Phase 7 step 1's smoke test (Groq unreachable) - not a regression, same pre-existing
  environmental limitation, and the fail-clean fallback handled it exactly as designed.

Known gaps carried forward, documented (not fixed) - see `ARCHITECTURE.md` §12 and the router/
service docstrings: no Transactional Outbox (state-write and publish aren't atomic), no Dapr-level
retry/dead-letter policy configured yet, `InMemoryInvoiceRepository` doesn't survive an Intake
restart (an in-flight `decision.completed` would then arrive for an unknown `tracking_id` -
`IntakeService.complete()` handles that by design: logs a warning, doesn't crash).

## Verification (Dapr state, step 4)

Design decisions made and verified for real, not just described:
- **`execute_state_transaction` confirmed present in the installed SDK, not just "per docs"** -
  read the actual installed `dapr==1.18.1` source directly: `dapr.aio.clients.DaprClient.execute_state_transaction`
  exists and takes `Sequence[TransactionalStateOperation]` (from `dapr.clients.grpc._request`,
  default `operation_type=upsert`) - this was flagged as the plan's biggest risk and is now closed.
- **Whole `Submission` (not just the dedup key) moved to Dapr state** - both written together in
  one atomic transaction (`submission:{tracking_id}` + `dedup:{dedup_key}` -> tracking_id). Storing
  only the dedup pointer while the full record stayed in-memory would have meant the pointer could
  survive an Intake restart while the record it points to did not - worse than today's fully
  in-memory approach, not better.
- **`LazyDaprClient` (`shared/dapr_client.py`)** extracted and retrofitted into
  `DaprDecisionPublisher`/`DaprDecisionOutcomePublisher` too (not just used for the new
  repository) - this was the third near-identical "build a Dapr client lazily" implementation,
  exactly the point earlier phases predicted would justify extraction. Takes a `factory:` callable
  (not hardcoded to `DaprClient`), enabling a deterministic laziness test (assert the factory was
  never called just from construction) instead of the earlier wall-clock-timing tests - all three
  laziness tests (publisher, outcome publisher, repository) now use this same mechanism.
- **Corrupted-state invariant decided explicitly**: if a dedup pointer exists but its Submission
  record doesn't (should be structurally impossible given the atomic transaction, but not assumed
  safe) - `find_by_dedup_key()` raises `InvoiceRepositoryError`, never returns `None` silently
  (silent `None` here would incorrectly mean "not a duplicate").
- **The write-time race (two near-simultaneous submits of the same invoice) is documented, not
  fixed** - it's not a "missing database" problem in general, it's specifically that Dapr state
  (Redis) has no native "insert only if absent" the way a PostgreSQL unique constraint does. Fix
  is deferred to the PostgreSQL migration, not an extension of Dapr state.

Ran for real:
- Full TDD cycle throughout (RED confirmed before every GREEN) - 171 tests total, ruff, and mypy
  all pass.
- `docker compose up --build -d` - all 7 containers healthy.
- Submitted a real invoice; `GET /invoices/{id}` returned `completed` via the Dapr-state-backed
  repository (not `InMemoryInvoiceRepository`).
- **The restart-survival test, strengthened per review to prove both halves of the state, not just
  one:**
  1. `docker compose restart intake` (single service) - **broke `intake-dapr`'s networking**:
     its logs showed repeated `dial tcp: lookup redis on 127.0.0.11:53: ... connection refused`.
     Root cause: `intake-dapr` uses `network_mode: "service:intake"`, borrowing `intake`'s network
     namespace (including its embedded DNS resolver) - restarting only `intake` disrupts that
     borrowed namespace out from under the sidecar, which was never itself restarted.
  2. Tried `docker compose restart intake intake-dapr` (both, simultaneously) - **`intake-dapr`
     crashed** (`Exited (1)`, fatal: "could not determine host IP address"): `restart` doesn't
     consult `depends_on`, so the sidecar can start before `intake`'s namespace is ready again.
  3. **`docker compose up -d --force-recreate intake intake-dapr`** - recreation (not restart)
     respects `depends_on` ordering - both came back healthy.
  4. `GET /invoices/{id}` for the original submission - **returned the full record correctly**,
     survived the recreation.
  5. Resubmitted the identical vendor/invoiceNumber/total - **completed immediately with
     `route: duplicate`, `is_duplicate: true`** - proves the dedup pointer survived too, not just
     the primary record. `decision`'s logs showed no new event for it (short-circuit still works).
  - **New operational finding, worth remembering**: for this `network_mode: service:X` sidecar
    layout, `docker compose restart <app>` is unsafe - always use
    `docker compose up -d --force-recreate <app> <app>-dapr` (or restart neither in isolation) to
    reliably bring the pair back after a code/config change.

---

# Phase 8 — Verification

## Goal

Create one command that proves correctness.

## Verification Command

```bash
docker compose run verification
```

## Required Journeys

- [ ] INV-1001 — Auto approve
- [ ] INV-1003 — Escalate and resume
- [ ] INV-1007 — Duplicate prevention
- [ ] INV-1012 — Payment failure compensation

## Anti-Cheese Guards

- [ ] "Approve me" payload does not change decision
- [ ] Forced agent approval above ceiling fails

---

# Phase 9 — Quality

## Tasks

- [ ] Structured logging
- [ ] Correlation id propagation
- [ ] Health checks
- [ ] Error handling
- [ ] Configuration management
- [ ] Provider abstraction

## Testing

- [ ] Unit tests
- [ ] Integration tests
- [ ] CI test execution

---

# Phase 10 — Documentation & Submission

## Tasks

- [ ] Complete README

README includes:

- Purpose
- Architecture diagram
- Technologies
- Local run instructions
- Testing instructions

- [ ] Add OpenAPI documentation
- [ ] Perform final architecture review
- [ ] Update MASTER_CHECKLIST.md
- [ ] Record demo video
- [ ] Freeze main branch

---

# Nice-To-Have

Implement only after all submission requirements are stable.

## High Value

- [ ] OpenTelemetry trace
- [ ] RAG policy retrieval
- [ ] Auth roles

## Medium Value

- [ ] Outbox pattern
- [ ] CD pipeline
- [ ] Additional integration tests

---

# Bonus

Only if all required work is complete.

- [ ] MCP Server
- [ ] Kubernetes manifests

---

# Development Rules

For every task:

1. Read relevant documentation.
2. Create or update tests.
3. Implement the smallest working change.
4. Run validation.
5. Update this plan.

Do not start future phases before the current critical path is stable.

---

# Critical Path

If time is limited, complete in this order:

1. Router
2. Decision Service
3. Four verification journeys
4. Docker Compose
5. README + Demo
6. Additional requirements