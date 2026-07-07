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
  delivery awaits the Notification service)

Implementation:
- [ ] API Gateway (not built yet - Intake currently the direct entry point)
- [x] Intake Service
- [ ] Dapr Pub/Sub (Intake -> Decision is plain HTTP today, by design - see ADR-worthy note in
  PLAN.md Phase 4: swapping to Dapr is a transport change, not structural)

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
- [ ] Same invoice cannot be paid twice (depends on the Payment service, not built yet)
- [x] Idempotency mechanism implemented for business-level duplicate submissions. **Documented
  gap**: `POST /invoices` is not idempotent at the transport level - a literal client retry
  produces a new `tracking_id` (though F3's dedup still catches it downstream, so no
  double-processing). A future `X-Idempotency-Key` would close this UX gap; not needed now since
  the business-level risk is already covered.

---

## Approver Flow

### F4 — Escalation queue

Priority: MUST HAVE

- [ ] Human review queue exists
- [ ] Only escalated items appear
- [ ] Agent recommendation visible
- [ ] Confidence score visible
- [ ] Policy rules cited

---

### F5 — Human decision + resume

Priority: MUST HAVE

- [ ] Approver can approve
- [ ] Approver can reject
- [ ] Approver can request more information
- [ ] Workflow pauses durably
- [ ] Workflow resumes after decision

---

### F6 — Avoid rubber stamping

Priority: MUST HAVE

- [ ] Low-risk items are automatically handled
- [ ] Human sees only necessary cases

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
- [ ] Payment outcome stored


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

- [ ] At least 3 services
- [ ] Each service containerized
- [ ] Clear service boundaries


## M4 — Docker Compose

- [ ] docker compose up starts system
- [ ] Databases included
- [ ] Queues included
- [ ] Infrastructure included


## M5 — Dapr

- [ ] Service invocation used
- [ ] Pub/Sub used
- [ ] Dapr state used
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

- [ ] Submission does not block
- [ ] Final result delivered asynchronously


## M9 — Payment Saga

- [ ] Payment flow implemented
- [ ] Failure scenario handled
- [ ] Compensation/rollback exists
- [ ] No partial payment


## M10 — Idempotency

- [ ] Duplicate submissions safe
- [ ] Redelivered events safe
- [ ] Payment retries safe


## M11 — Durable HITL

- [ ] Human approval survives restart
- [ ] Workflow state persisted


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
- [ ] Sequence diagram
- [ ] Payment flow diagram
- [ ] Compensation flow


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
- [ ] Payment failure compensation passes
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