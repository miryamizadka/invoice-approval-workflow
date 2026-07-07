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

## Current Focus

Phase 1, Phase 2 (Decision Service), and Phase 3 (AI Agent Integration) are all complete at the
code/test level. Remaining before Phase 3 is fully "production-ready": run
`scripts/smoke_test_groq_strict.py` manually against the real Groq API (blocked in the current
dev sandbox by a network/SSL restriction, not by the code). Next: connect Intake -> Decision
Service (still synchronous HTTP only; Dapr pub/sub as a second transport is explicitly deferred -
`build_decider()` already exists as the seam it will use), or start Phase 5 (Approval Service).

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

- [ ] Create Intake Service
- [ ] Create invoice submission endpoint
- [ ] Generate tracking id
- [ ] Implement asynchronous processing
- [ ] Persist submission state
- [ ] Add status endpoint

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

- [ ] Create Docker Compose environment
- [ ] Containerize services
- [ ] Add PostgreSQL
- [ ] Add Redis
- [ ] Configure Dapr
- [ ] Configure Pub/Sub communication
- [ ] Configure Dapr state
- [ ] Configure Dapr secrets

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