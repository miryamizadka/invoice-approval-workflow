# ADR-005: Technology Stack

**Status:** Accepted

## Context
The system needs a language and framework that support both an AI agent and Dapr, and let us build and ship a multi-service system within the deadline.

## Decision
- **Python** as the primary language - the richest ecosystem for LLMs and agents, with Dapr support.
- **FastAPI** as the web framework - async, and auto-generates the OpenAPI spec (D4).
- **LangGraph** for the agent - stateful workflow with checkpointing, a good fit for the reasoning + HITL flow.
- **Free-tier LLM (Groq / Gemini)** behind a swappable provider interface, with a stub for CI (M15).

## Consequences

**Pros:**
- Strong AI/agent tooling and fast development.
- OpenAPI comes for free from FastAPI (D4).
- The LLM is swappable and CI stays deterministic via the stub.

**Cons:**
- Python is less strongly typed than C#; we mitigate with type hints and MyPy.

## Alternatives considered
- **C# / .NET** - rejected; strong Dapr support but a weaker AI/agent ecosystem than Python.
- **MAF (Microsoft Agent Framework)** - considered; integrates well with Dapr, but LangGraph was chosen for its wider industry adoption, larger community, and more available examples - valuable under a deadline.
