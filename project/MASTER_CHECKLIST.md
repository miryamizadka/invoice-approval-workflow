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

- [ ] Submit invoice/expense endpoint exists
- [ ] Immediate acknowledgement returned
- [ ] Tracking id generated
- [ ] Processing happens asynchronously
- [ ] Final result delivered later

Implementation:
- API Gateway
- Intake Service
- Dapr Pub/Sub

---

### F2 — Status + plain-language reason
Priority: MUST HAVE

- [ ] Status endpoint exists
- [ ] User can retrieve submission status
- [ ] Final decision includes understandable reason

---

### F3 — Duplicate prevention
Priority: MUST HAVE

- [ ] Duplicate submissions detected
- [ ] Same invoice cannot be paid twice
- [ ] Idempotency mechanism implemented

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

- [ ] Structured logs
- [ ] Correlation id everywhere


## M15 — Code Quality

- [ ] Clean architecture
- [ ] Error handling
- [ ] Health checks
- [ ] Separation of concerns
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
- [ ] Integration tests


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

- [ ] OpenAPI available
- [ ] Swagger / Scalar available


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
- [x] LLM Provider Abstraction (implementation + 18 unit tests, ruff/mypy clean)

Current:

- [ ] LangGraph agent/graph, and/or Decision Service (FastAPI wrapper around router + agent)

Next:

- [ ] Complete Decision Service
- [ ] Build verification journeys
- [ ] Connect services