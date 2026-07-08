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

In Progress

- Manual Groq strict-mode smoke-test (script ready; blocked both on the host and, now confirmed,
  inside the Docker containers by the same sandbox SSL-intercepting network egress - needs a
  network without that restriction to fully verify Groq's strict-mode structured output)

Planned

- Approval Service
- Payment Saga
- Notification
- UI
- Dapr (pub/sub, state, secrets - Postgres/Redis already running in compose, unused, reserved
  for this)
- CI
- Verification
- Demo

---

# Development Principles

- Architecture decisions are frozen.
- Follow ADRs.
- Implement one feature at a time.
- Tests before implementation whenever possible.
- Prefer deterministic logic over LLM decisions.
- Never bypass the router.