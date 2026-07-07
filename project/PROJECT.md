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
- LLM Provider Abstraction (implementation + tests, ruff/mypy clean)

In Progress

- LangGraph agent/graph
- Decision Service implementation

Planned

- Approval Service
- Payment Saga
- Notification
- UI
- Docker Compose
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