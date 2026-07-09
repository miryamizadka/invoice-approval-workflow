# ApprovalFlow – Master Checklist

## Submission Goal

Deliver a working, demonstrable AI-assisted approval platform.

Priority order:

1. Functional correctness
2. Must-have requirements
3. Architecture compliance
4. Tests and verification
5. Documentation
6. Nice-to-have improvements
7. Bonus features

---

# 1. Functional Requirements

## Submitter Flow

### F1 — Async invoice submission + tracking id
Priority: MUST HAVE

- [x] Submit invoice/expense endpoint exists (`POST /invoices`, `services/intake/app.py`)
- [x] Immediate acknowledgement returned (202 Accepted, before processing starts)
- [x] Tracking id generated (UUID, also used as `correlation_id`)
- [x] Processing happens asynchronously (FastAPI `BackgroundTasks` - explicitly documented as a
  temporary stand-in for Dapr pub/sub, not a durable production queue; acceptable for this phase)
- [x] Final result delivered later (retrievable via `GET /invoices/{tracking_id}`; push-style
  delivery via Notification Service - see M8)

Implementation:
- [ ] API Gateway (not built yet - Intake currently the direct entry point)
- [x] Intake Service
- [x] Dapr Pub/Sub (Intake -> Decision now goes over `invoice.submitted`/`decision.completed`,
  not direct HTTP - see PLAN.md Phase 7 step 3)

---

### F2 — Status + plain-language reason
Priority: MUST HAVE

- [x] Status endpoint exists (`GET /invoices/{tracking_id}`)
- [x] User can retrieve submission status
- [x] Final decision includes understandable reason (`Decision.reason`, surfaced via
  `SubmissionStatusResponse.reason`; a generic safe message on internal failure instead of
  leaking raw error text)

---

### F3 — Duplicate prevention
Priority: MUST HAVE

- [x] Duplicate submissions detected (`compute_dedup_key`, checked against the repository in
  `IntakeService.submit()`, `is_duplicate` passed through to Decision Service)
- [x] Same invoice cannot be paid twice (`PaymentRecord` keyed by `tracking_id`, idempotent
  redelivery guard in `PaymentService._run_saga()` - a redelivered `decision.completed`/
  `approval.completed` for an already-terminal payment is a no-op. Verified live: resubmitting
  the same triggering event does not double-charge or double-publish)
- [x] Idempotency mechanism implemented for business-level duplicate submissions. **Documented
  gap**: `POST /invoices` is not idempotent at the transport level - a literal client retry
  produces a new `tracking_id` (though F3's dedup still catches it downstream, so no
  double-processing). A future `X-Idempotency-Key` would close this UX gap; not needed now since
  the business-level risk is already covered.

---

## Approver Flow

### F4 — Escalation queue

Priority: MUST HAVE

- [x] Human review queue exists (`GET /approvals`, `services/approval/app.py`)
- [x] Only escalated items appear (`ApprovalService.handle_decision_completed()` ignores every
  route except `human_review`; verified live - an auto_approve invoice never reaches the queue)
- [x] Agent recommendation visible (`PendingApproval.recommendation`, required a `DecisionOutcome`/
  `DecisionCompletedEvent` enrichment - see PLAN.md Phase 5)
- [x] Confidence score visible (`recommendation.confidence`, same object)
- [x] Policy rules cited (`PendingApproval.decision.reason` / `.triggered_rules`)

---

### F5 — Human decision + resume

Priority: MUST HAVE

- [x] Approver can approve (`POST /approvals/{tracking_id}/approve`)
- [x] Approver can reject (`POST /approvals/{tracking_id}/reject`)
- [x] Approver can request more information (`POST /approvals/{tracking_id}/request-info` ->
  `WAITING_INFO`, non-terminal. **Documented gap**: no Gateway/UI route yet for the submitter to
  actually supply that information and trigger a resume - accepted, out of scope until M7)
- [x] Workflow pauses durably (`DaprStateApprovalRepository`, Dapr state/Redis - see M11 below)
- [x] Workflow resumes after decision (approve/reject transition `PENDING`/`WAITING_INFO` ->
  terminal and publish `approval.completed`; verified live including resuming a `WAITING_INFO`
  item that survived a container restart)

---

### F6 — Avoid rubber stamping

Priority: MUST HAVE

- [x] Low-risk items are automatically handled (router's AUTO_APPROVE path, unchanged - Approval
  never sees these)
- [x] Human sees only necessary cases (only `route=human_review` items reach the queue), **and**
  sees the reasoning behind why (confidence score + cited policy rules, F4) - the core anti-
  rubber-stamping mechanism: without visible confidence/rules, the human can't judge whether to
  trust or override the recommendation.

---

## Finance / Controller

### F7 — Configurable policy and thresholds

Priority: MUST HAVE

- [ ] Policy is external configuration
- [ ] Thresholds can change without redeployment

---

### F8 — Dashboard

Priority: NICE TO HAVE

- [ ] Auto approval rate
- [ ] Human escalation rate
- [ ] Money auto-approved
- [ ] Money human-approved


---

## Auditor

### F9 — Complete decision trail

Priority: MUST HAVE

- [ ] Correlation id exists
- [ ] Invoice extraction stored
- [ ] Rules applied stored
- [ ] Agent recommendation stored
- [ ] Final decision stored
- [x] Payment outcome stored (`PaymentRecord` - status, reason, reserved_amount - persisted via
  `DaprStatePaymentRepository`, keyed by `tracking_id`; `GET /payments/{tracking_id}`)


---

### F10 — Prove autonomy limit

Priority: MUST HAVE

- [ ] Router enforces ceiling
- [ ] Agent cannot bypass router
- [ ] Forced approve recommendation above limit fails


---

# 2. Must-Have Technical Requirements

## Repository

### M1 — Private monorepo

- [ ] Single GitHub repository
- [ ] Everything needed exists in repository


### M2 — GitHub Flow

- [ ] main contains submission version
- [ ] Features developed in branches
- [ ] No secrets committed
- [ ] .gitignore exists
- [ ] LICENSE exists
- [ ] .env.example exists


---

# Architecture

## M3 — Microservices

- [x] At least 3 services (Intake, Decision, Approval, Payment, Notification)
- [x] Each service containerized (single shared `Dockerfile`, one container per service)
- [x] Clear service boundaries (HTTP only between Intake and Decision, no cross-service imports)


## M4 — Docker Compose

- [x] docker compose up starts system (verified: `docker compose up --build`, all 4 containers
  came up healthy; `scripts/smoke_test_compose.py` proved Intake -> Decision over the Docker
  network end to end)
- [x] Databases included (`postgres:16-alpine`, running, not yet consumed by a service - reserved
  for Dapr state, a later phase)
- [x] Queues included (`redis:7-alpine`, running, not yet consumed - reserved for Dapr pub/sub)
- [x] Infrastructure included (both above run unconditionally in the main compose file, not
  behind `profiles:`, per M4's literal "including queues, databases, and supporting
  infrastructure")


## M5 — Dapr

Infra step 1 done: `daprd` sidecar per service + `placement`, `dapr/components/{pubsub,statestore}.yaml`
backed by Redis, verified via `/v1.0/healthz` + `/v1.0/metadata` + logs (see PLAN.md Phase 7).
Step 3 done: real pub/sub between Intake and Decision, Decision and Approval, and now Decision/
Approval and Payment too, verified over the actual Docker network (`docker compose logs` shows
`POST /events/invoice-submitted`/`POST /events/decision-completed`/`POST /events/approval-completed`,
zero direct HTTP calls between any of these four services). Step 4 done: Dapr state now backs
Intake's, Approval's, and Payment's repositories - including Payment's budget reservation via
ETag optimistic concurrency (INV-1014), not just simple save/get. Service invocation and Dapr
secrets remain unused.

- [ ] Service invocation used
- [x] Pub/Sub used (`invoice.submitted` published by Intake via `DaprDecisionPublisher`;
  `decision.completed` published by Decision via `DaprDecisionOutcomePublisher`; `approval.completed`
  published by Approval via `DaprApprovalOutcomePublisher`; `payment.completed` published by
  Payment via `DaprPaymentOutcomePublisher`; all subscribed to via `dapr-ext-fastapi`'s `DaprApp`)
- [x] Dapr state used (`DaprStateInvoiceRepository` for Intake, `DaprStateApprovalRepository` for
  Approval, `DaprStatePaymentRepository`/`DaprStateBudgetRepository` for Payment - the same
  append-only-index pattern for records; budgets additionally use ETag-based optimistic
  concurrency, backed by the `statestore` component)
- [ ] Dapr secrets used


## M6 — API Gateway

- [ ] Single external entry point
- [ ] Rate limiting implemented


## M7 — Minimal UI

- [ ] Submit invoice
- [ ] View status
- [ ] View decision


---

# Workflow Correctness

## M8 — Async Processing

- [x] Submission does not block (`POST /invoices` returns 202 immediately; processing continues
  in a `BackgroundTasks` job, unchanged since M5)
- [x] Final result delivered asynchronously (Notification Service - `decision.completed`
  [reject/duplicate], `approval.completed` [rejected], `payment.completed` [both outcomes] all
  converge on a push notification; every terminal outcome now reaches the submitter, not just
  pull via `GET /invoices/{id}`. Verified live for all four fixture journeys - INV-1003, INV-1007,
  INV-1012, INV-1015 - plus redelivery idempotency)


## M9 — Payment Saga

- [x] Payment flow implemented (`PaymentService` - orchestration-style saga, ADR-004: reserve
  department budget, then execute payment; Payment is the sole coordinator)
- [x] Failure scenario handled (`SimulatedPaymentGateway` deterministically declines
  `PAYMENT_SIMULATE_FAILURE_IDS`-listed invoices [`INV-1012` in docker-compose.yml] - verified
  live: `status: failed` with the decline reason, no orphaned reservation)
- [x] Compensation/rollback exists (`BudgetRepository.release()` on gateway failure, crediting
  back exactly the `reserved_amount` stored at reservation time - verified live via `docker
  compose logs payment`'s `budget_reserved` → `payment_failed_compensated` sequence and the
  department budget returning to its exact pre-submission baseline)
- [x] No partial payment (insufficient-budget rejections happen before any gateway call at all -
  verified `gateway.charged == []` in that path; INV-1014A/B concurrency script confirms exactly
  one of a concurrent pair ever succeeds, budget never goes negative, across multiple iterations)


## M10 — Idempotency

- [x] Duplicate submissions safe - `DaprStateInvoiceRepository` (Dapr state/Redis) backs the
  dedup check, survives an Intake restart (verified via a real `docker compose` restart +
  resubmission - see PLAN.md Phase 7 "Verification (Dapr state, step 4)"). Known, accepted gap:
  no protection against two near-simultaneous submissions of the exact same invoice racing the
  check-then-save window (documented, deferred to a future PostgreSQL unique constraint).
- [x] Redelivered events safe - `IntakeService.complete()` is idempotent for `decision.completed`
  redelivery (already-completed tracking_id -> no-op; unknown tracking_id -> logged, not a crash)
- [x] Payment retries safe - `PaymentService._run_saga()` checks for an existing terminal
  `PaymentRecord` before acting (no-op if found); a crash between reserve and charge leaves the
  record at `RESERVED`, so a redelivery resumes exactly at the charge step instead of
  re-reserving. Verified live via `docker compose` restart (record + budget both survived,
  `ensure_seeded()` did not reset the in-progress budget) and via two dedicated deterministic
  unit tests for the RESERVED-resume path (success and failure outcomes).
- [x] Notification retries safe - `NotificationService._notify()` checks a Dapr-state
  tracking_id marker before sending (no-op if already notified); `send()` runs before
  `mark_notified()` so a failed send is safely retried. Verified live: re-POSTing an identical
  `payment.completed` event body a second time (simulating Dapr redelivery) logged
  `notification_already_sent_skipping` with no second `notification_delivered` line.


## M11 — Durable HITL

- [x] Human approval survives restart - verified live: `docker compose up -d --force-recreate
  approval approval-dapr` mid-flow (one item `approved`, one `waiting_info`); both containers came
  back healthy and `GET /approvals` showed both items with their pre-restart statuses intact
  (see PLAN.md Phase 5 "Verification")
- [x] Workflow state persisted (`DaprStateApprovalRepository`, Dapr state/Redis, same pattern as
  Intake's `DaprStateInvoiceRepository`) - and proven still fully actionable post-restart, not
  just readable: successfully called `approve` on the surviving `waiting_info` item afterward


---

# AI Decisioning

## M12 — Deterministic Router

Priority: CRITICAL

- [x] Agent only recommends (`Recommendation` model has no route/approval field, `extra="forbid"`)
- [x] Router makes final decision (`services/decision/router/router.py::route_decision`)
- [x] Ceiling enforced in code (AUTONOMY-CEILING, boundary-tested at $250.00/$250.01)
- [x] Confidence threshold enforced (AUTONOMY-CONFIDENCE, boundary-tested at 0.80/0.79)
- [x] Hard stops enforced (GLOBAL-VENDOR, GLOBAL-FX, GLOBAL-RECEIPT, GLOBAL-MATH)
- [x] Anti-prompt injection tested (INV-1013 fixture + notes-mutation test; router never reads `invoice.notes`)


## M13 — External Configuration

- [ ] Policy configurable
- [ ] Threshold configurable
- [ ] No hardcoded limits


---

# Cross Cutting

## M14 — Observability

- [x] Structured logs (`services/decision/service/logging_config.py`, JSON, stdlib-only)
- [x] Correlation id everywhere - within Decision Service (`X-Correlation-Id` header, generated
  if absent, on every log line and in the `Decision` response); not yet propagated across other
  services since only Decision Service exists so far

## M15 — Code Quality

- [ ] Clean architecture (true for Decision Service; not yet evidenced system-wide)
- [x] Error handling (`AgentError` -> documented `human_review` fallback, never a crash; a
  global exception handler for genuinely unexpected errors, logged with correlation_id)
- [x] Health checks (`GET /health`, deliberately no LLM connectivity check - see decision plan)
- [x] Separation of concerns (`Decider.decide()` has zero FastAPI/HTTP imports; `build_decider()`
  is the composition seam a future Dapr transport reuses without touching the HTTP layer)
- [x] LLM provider abstraction (`LLMProvider` Protocol, swappable via `LLM_PROVIDER` env var)
- [x] Provider failure handling (fail-fast on missing key; SDK/empty-completion errors wrapped
  in `LLMProviderError`, never silent)


## M16 — CI

- [ ] CI pipeline exists
- [ ] Runs on push
- [ ] Quality gates


## M17 — Automated Tests

- [ ] Tests run in CI
- [x] Router tests (69 unit tests: fixture-driven + per-rule boundaries)
- [x] LLM provider tests (18 unit tests: MockProvider, GroqProvider with a fake client, factory)
- [x] Integration tests (`tests/integration/test_decision_service.py`, 9 tests: full HTTP ->
  Decider -> agent -> router chain via `TestClient` + `MockProvider`/a stub;
  `tests/integration/test_intake_service.py`, 8 tests: full HTTP -> IntakeService ->
  InMemoryInvoiceRepository via `TestClient` + a stub `DecisionServiceClient`)
- [x] Intake unit tests (`tests/unit/intake/test_repository.py`, 5 tests: save/get roundtrip,
  dedup-key lookup); 128 tests total in the suite


## M18 — README

- [ ] Project explanation
- [ ] Technology list
- [ ] Run instructions
- [ ] Test instructions
- [ ] System diagram


---

# 3. Development Process

## D1 — Architecture Documentation

- [x] ARCHITECTURE.md exists
- [x] Sequence diagram (§11 - Escalate and Resume, INV-1003)
- [x] Payment flow diagram (§11 - Payment Flow with Compensation, Journey D/INV-1012)
- [x] Compensation flow (same diagram - RES→PAY→COMP→FAILED path)


## D2 — ADRs

- [x] ADR directory exists
- [x] ADR decisions documented


## D3 — Git Hygiene

- [ ] GitHub Flow followed
- [ ] Clean repository


## D4 — API Documentation

- [x] OpenAPI available (auto-generated by FastAPI from the existing Pydantic models, at
  `/openapi.json`)
- [x] Swagger / Scalar available (FastAPI's default Swagger UI at `/docs`, no extra config)


## D5 — Verification

CRITICAL

- [ ] Single command runs verification
- [ ] Auto approve scenario passes
- [ ] Human escalation passes
- [ ] Duplicate scenario passes
- [x] Payment failure compensation passes (INV-1012 verified live via docker compose - see
  PLAN.md Phase 6 "Verification"; not yet wired into a single automated verification command,
  that's D5's still-pending "Single command runs verification" item)
- [ ] Anti-cheese test passes


## D6 — README

- [ ] Complete README


## D7 — Demo

- [ ] 2-5 minute recording
- [ ] Working flow demonstrated


---

# 4. Nice To Have

## N1 Auth

- [ ] JWT authentication
- [ ] Roles:
  - Submitter
  - Approver
  - Admin


## N2 CD

- [ ] Automatic artifact publishing


## N3 Reliability

- [ ] Outbox pattern
- [ ] Bulkhead
- [ ] Throttling


## N4 OpenTelemetry

- [ ] Metrics
- [ ] Distributed tracing


## N5 RAG

- [ ] Policy indexed
- [ ] Relevant clauses retrieved


## N6 Testing Layers

- [ ] Unit tests
- [ ] Integration tests
- [ ] End-to-end tests


---

# 5. Bonus

## B1 Evaluation Harness

- [ ] Dataset evaluation
- [ ] Metrics report


## B2 MCP Server

- [ ] Custom MCP server
- [ ] Agent tools exposed through MCP


## B3 Kubernetes

- [ ] Kubernetes manifests


---

# Current Status

Completed:

- [x] Architecture
- [x] ADRs
- [x] Product Dilemma
- [x] Decision Models
- [x] Deterministic Router (implementation + 69 unit tests, ruff/mypy clean)
- [x] LLM Provider Abstraction, incl. Groq strict structured-output support
  (implementation + 23 unit tests, ruff/mypy clean)
- [x] LangGraph Agent (`preprocess`/`classify`/`router` nodes, DI'd provider+thresholds;
  implementation + tests, ruff/mypy clean)
- [x] Decision Service (`services/decision/service/`: `Decider`/`build_decider` transport-agnostic
  core + FastAPI wrapper; structured logging, correlation-id, global exception handler;
  implementation + integration tests, ruff/mypy clean)
- [x] shared/contracts/ extraction (Invoice/Decision/Recommendation/enums/`compute_dedup_key` -
  the single source both Decision and Intake depend on; pure refactor, all 113 prior tests
  passed unmodified before Intake was built on it)
- [x] Intake Service (`services/intake/`: `IntakeService`/`build_intake_service`
  transport-agnostic core, `InvoiceRepository`/`InMemoryInvoiceRepository`,
  `DecisionServiceClient`/`HttpDecisionServiceClient`, thin FastAPI wrapper; implementation +
  13 tests, ruff/mypy clean; 128 tests total in the suite)

Current:

- [ ] Manual smoke-test of GroqProvider strict mode against the real API
  (`scripts/smoke_test_groq_strict.py` - written, blocked by a sandbox network/SSL
  restriction, needs running in an environment with real access to api.groq.com)

Next:

- [ ] Connect Intake -> Decision Service (still out of scope: no Intake service yet)
- [ ] Build verification journeys
- [ ] Connect services