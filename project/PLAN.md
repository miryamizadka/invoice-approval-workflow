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

## Current Focus

Phase 1, Phase 2 (Decision Service), Phase 3 (AI Agent Integration), Phase 4 (Intake Service),
and Phase 7 step 1 (Docker Compose) are all complete and verified. Remaining before Phase 3 is
fully "production-ready": run `scripts/smoke_test_groq_strict.py` manually against the real Groq
API from a network that isn't behind an SSL-intercepting proxy (confirmed blocked both on the
host and now inside the Docker containers too - see Phase 7 notes; environmental, not a code
issue). Next: Phase 5 (Approval Service), or wiring Dapr pub/sub as Intake<->Decision's second
transport (both services already expose the composition seams - `build_decider()`,
`build_intake_service()` - this needs; Postgres/Redis are already running in compose, unused,
reserved for this).

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
  pattern as `LLMProvider`; a future `PostgresInvoiceRepository` swaps in without touching
  `IntakeService`)
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

- [ ] Create Approval Service
- [ ] Create human review queue
- [ ] Display:
  - Invoice data
  - Agent recommendation
  - Confidence score
  - Policy reasons

- [ ] Implement approve action
- [ ] Implement reject action
- [ ] Implement request-information action
- [ ] Implement durable pause/resume

---

# Phase 6 — Payment Flow

## Goal

Demonstrate reliable transaction handling.

Required scenario:

INV-1012 — payment failure + compensation.

## Tasks

- [ ] Create Payment Service
- [ ] Implement payment reservation
- [ ] Implement failure simulation
- [ ] Implement compensation action
- [ ] Document Saga flow

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
- [ ] Configure Dapr state - wiring idempotency/dedup and HITL pause-resume to the `statestore`
  component is a separate, future step (infra is ready for it)
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