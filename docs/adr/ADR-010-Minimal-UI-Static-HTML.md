# ADR-010: Minimal UI as Static HTML/CSS/Vanilla JS, Served Through the Gateway

**Status:** Accepted

## Context

M7 ("Minimal UI", MASTER_CHECKLIST.md) requires: submit an invoice, view its status, view its decision. No UI existed at all before this phase. `ARCHITECTURE.md` §4's own description of "UI (M7)" is broader than the three checklist bullets: it also commits to letting an approver act on the escalation queue (F4, F5) - a decision already made in the architecture doc, not a new scope choice here.

Investigating F5 ("approve/reject/send back for more info and resume exactly where it paused") found that the "resume" part was already fully implemented: `PendingApproval.status` treats `WAITING_INFO` as non-terminal, and `approve`/`reject` already operate on it identically to `PENDING` (`services/approval/models.py`'s own state-machine docstring). The only real gap was a channel for the *submitter* to respond - not a new workflow mechanism.

## Decision

**Technology: static HTML + CSS + vanilla JS, no framework, no build step.** `pyproject.toml` has neither `jinja2` nor `aiofiles` - Jinja2Templates has never been used in this project. `StaticFiles` (built into Starlette, already a FastAPI dependency) needs no new dependency at all. `ARCHITECTURE.md` §6 left frontend technology unspecified - this is the most minimal choice that satisfies it.

**Why not React/Vue:**
- A build step (npm/webpack/vite) is unnecessary for two static pages with no client-side routing.
- The backend already exposes a complete REST API - the UI's entire job is to *display* it, not reimplement business logic.
- Fewer dependencies means less to install, build, and keep updated.
- `CLAUDE.md` explicitly says to avoid unnecessary frameworks.

**Architectural note:** the static UI mirrors the existing public REST API directly - a future migration to React/Vue would replace only the frontend implementation, without touching any backend contract.

**Hosting:** a new `services/ui` FastAPI app - `app.mount("/", StaticFiles(...), name="static")` plus `/health` - is itself "logic free", the same posture `ARCHITECTURE.md` already mandates for the Gateway. It is served through Traefik at `/ui`, same-origin with every other route (`localhost:8080`), so the browser's own `fetch()` calls to `/invoices`, `/approvals`, etc. need no CORS configuration at all (confirmed: no CORS exists anywhere in this project).

Because `services/ui`'s `StaticFiles` mount is at `/` and doesn't know its own external mount path, a `stripprefix-ui` Traefik middleware (scoped to only the `ui` router) strips `/ui` before forwarding - keeping the service itself decoupled from where it happens to be served, consistent with every other service not knowing about its own gateway prefix.

**Scope: approver view included, dashboard excluded.** Per `ARCHITECTURE.md` §4's own wording, not a new interpretation. F8 (Dashboard) stays NICE TO HAVE and out of scope - it isn't one of M7's three checklist bullets, and building it now would be scope creep beyond what either M7 or F8's own priority label calls for.

**F5 closed in full, not left partial.** Initially scoped as "out of scope, needs a new backend endpoint" - re-examined once the state-machine finding above surfaced (a partial F5 - approve/reject working, no submitter channel - is exactly the kind of gap that shows up in a live demo). The fix is small: one new field, one new endpoint, one new state transition:

```
PENDING ──approve──────► APPROVED
   │
   ├──reject────────────► REJECTED
   │
   └──request_info──────► WAITING_INFO ──approve────────► APPROVED
                                        ├─reject──────────► REJECTED
                                        └─additional_info─► PENDING   (new)
```

`PendingApproval.additional_info: str | None` stores the submitter's response. `POST /approvals/{tracking_id}/additional-info` is valid **only** from `WAITING_INFO` (409 otherwise - this endpoint's purpose is responding to a specific request, not general note-taking) and transitions to `PENDING` - not because the mechanism to resolve needed unblocking (it never was blocked), but because the approver needs a **visible signal** that new information has arrived, distinguishable from "still waiting, no response yet". `approve`/`reject` on `WAITING_INFO` are completely unchanged - this is an added edge on the existing diagram, not a rewrite. `additional_info` is never cleared by a later approve/reject, so it stays visible on the resolved item.

The submitter's page detects `waiting_info` by additionally polling `GET /approvals/{tracking_id}` (an existing endpoint - Intake's own status has no visibility into Approval's internal state) alongside its normal `GET /invoices/{tracking_id}` polling.

**Browser support:** modern Chromium-based browsers only. No IE support, no polyfills - consistent with the project's minimal-tooling posture.

## Consequences

**Pros:**
- M7 and F5 are both genuinely, fully satisfied - not "F5 mostly done."
- Zero new dependencies.
- The UI is a thin, disposable layer - replacing it later touches no backend contract.

**Cons:**
- No client-side routing/SPA niceties (full page loads between the two pages) - acceptable for two pages.
- No automated browser-level tests (see "Testing" below).

## Testing

Python-level tests cover only what there is to test in Python: the `services/ui` app is a static-file mount, so its own tests (`tests/integration/test_ui_service.py`) check that the right files are served with the right content-type - equivalent to testing the Gateway's own "logic free" routing, not application logic. There is no browser-automation framework (Playwright/Selenium) in this project (N6 "Testing Layers" remains an unimplemented bonus item) - setting one up was judged out of scope for a minimal UI pass. The actual HTML/JS behavior was verified by (a) reading the source directly for correctness, (b) exercising every endpoint the JS calls directly via `httpx`/`curl` through the live gateway (submit → poll → decision, full F5 request-info → additional-info → approve → payment flow, tracking-id-after-refresh), and (c) a manual visual/interactive pass in an actual browser, performed by the project owner - this agent has no browser tool available and said so explicitly rather than claiming a visual verification it could not perform.

## Alternatives considered

- **Jinja2Templates (server-rendered)** - rejected; would add a new dependency for no real benefit over static HTML + client-side `fetch()`, given the backend is already a clean REST API.
- **React/Vue SPA** - rejected; see "Why not React/Vue" above.
- **Leaving F5's resume gap open, documented only** - rejected after finding the actual gap (a submitter channel) was much smaller than originally assumed (no new workflow mechanism, no new event) - the cost of closing it properly was low enough, and the risk of a visibly-incomplete MUST-HAVE requirement in a demo was real enough, to close it now.
