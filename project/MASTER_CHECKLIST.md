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
- [x] API Gateway (Traefik - see M6)
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
  `WAITING_INFO`, non-terminal)
- [x] Workflow pauses durably (`DaprStateApprovalRepository`, Dapr state/Redis - see M11 below)
- [x] Workflow resumes after decision (approve/reject transition `PENDING`/`WAITING_INFO` ->
  terminal and publish `approval.completed`; verified live including resuming a `WAITING_INFO`
  item that survived a container restart)
- [x] **Submitter can supply the requested information and resume** - previously a documented
  gap; closing it turned out to need only a small addition, since `approve`/`reject` already
  worked on `WAITING_INFO`: `PendingApproval.additional_info` field + `POST
  /approvals/{tracking_id}/additional-info` (409 unless status is `WAITING_INFO`) transitions
  back to `PENDING` - a visible signal to the approver that new information has arrived, not a
  new resolution mechanism. Exposed via the M7 UI's submitter page (polls `GET
  /approvals/{tracking_id}` alongside its normal status polling). Verified live end to end:
  `request-info` -> submitter sees `waiting_info` -> `additional-info` -> back to `pending` with
  the text visible -> `approve` -> payment completed. See ADR-010.

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

- [x] Policy is external configuration (`services/decision/service/dapr_config_loader.py` -
  policy text read from a Dapr `configuration.redis` store at startup, falling back to
  `policy/policy.md` if unset/unreachable - see ADR-009)
- [x] Thresholds can change without redeployment (`AutonomyThresholds`' 8 fields, same mechanism -
  verified live: `redis-cli SET ceiling 500` + `docker compose restart decision`, no image
  rebuild, flipped a $300 invoice's `AUTONOMY-CEILING` gate from firing to not firing; reverted
  and confirmed the original behavior - including `verify_phase8`'s full suite, INV-1013's
  anti-cheese ceiling test included - still passes)

---

### F8 — Dashboard

Priority: NICE TO HAVE

`GET /audit/summary` (Audit service) groups `audit_trail` by `(route, currency,
approval_resolution)` in Postgres; `AuditService.get_summary()` turns the raw buckets into
the four metrics below. Rendered on a new static page (`services/ui/static/dashboard.html`),
polling every 10s, following M7's existing page pattern - no charting library, text/number
tiles only. Verified live: ran `verify_phase8` twice back-to-back and confirmed every number
(`total_invoices`, money per currency) exactly doubled between runs - proof the dashboard
reflects real cumulative Postgres data, not a cached/static response.

- [x] Auto approval rate (`auto_approval_rate` = `auto_approved_count / total_invoices`,
  `0.0` on an empty deployment rather than a divide-by-zero)
- [x] Human escalation rate (`human_escalation_rate`, same shape)
- [x] Money auto-approved (`money_auto_approved`, per-currency dict - invoices in this system
  are not all one currency; a naive cross-currency `SUM` would be meaningless)
- [x] Money human-approved (`money_human_approved` - specifically `route=human_review AND
  approval_resolution=approved`, not "any human-touched money" and not tied to payment
  success [that's Payment/M9's separate concern]; a human_review invoice that was rejected
  still counts toward the escalation rate but never appears here - verified with a dedicated
  unit test for exactly this distinction, plus a mixed-currencies+rejected combined test)


---

## Auditor

### F9 — Complete decision trail

Priority: MUST HAVE

- [x] Correlation id exists (`audit_trail.tracking_id`, primary key - `services/audit/models.py`)
- [x] Invoice extraction stored (`invoice_json` + flattened columns - `services/audit/repository.py`)
- [x] Rules applied stored (`triggered_rules`, from `Decision.triggered_rules`)
- [x] Agent recommendation stored (`recommendation_*` columns - now populated for **every** route the
  agent ran for, including `auto_approve`/`reject`, not just `human_review` - closes a real gap:
  before this phase, `recommendation` was persisted only by Approval's `PendingApproval`, and only
  for `human_review` items; verified live against a real `auto_approve` invoice, INV-1001)
- [x] Final decision stored (`route`/`decision_reason`)
- [x] Payment outcome stored (`payment_resolution`/`payment_reason` in `audit_trail`, in addition to
  `PaymentRecord` - status, reason, reserved_amount - persisted via `DaprStatePaymentRepository`,
  keyed by `tracking_id`; `GET /payments/{tracking_id}`)

Implemented by a new **Audit service** (F9, ADR-008): a pure, observational consumer of the existing
`decision.completed`/`approval.completed`/`payment.completed` events, projecting each into one row
per `tracking_id` in a new PostgreSQL `audit_trail` table - the first real consumer of the
previously-unused `postgres` container. `GET /audit/{tracking_id}` exposes the full trail, routed
through the M6 gateway like every other service. Verified live via `docker compose up --build`:
all four invoice journeys (auto_approve/INV-1001, human_review/INV-1003, payment-failure/INV-1012,
duplicate) produce a correct, complete trail; `psql`-level `GROUP BY route` aggregation confirmed
working directly against real SQL; Audit stopped mid-flow does not block Approval/Payment/
Notification from completing, and backfills automatically once restarted (Dapr redis-streams
redelivery) - no data lost. See `docs/adr/ADR-008-Audit-Trail-Direct-Postgres-Access.md` and
`project/PLAN.md`'s Phase 9.


---

### F10 — Prove autonomy limit

Priority: MUST HAVE

- [x] Router enforces ceiling (`services/decision/router/router.py` gate 4 -
  `invoice.total > thresholds.ceiling` runs unconditionally, before the agent-signal gate)
- [x] Agent cannot bypass router (`tests/unit/decision/test_router.py:66`
  `test_m12_ceiling_survives_optimistic_agent`, `:81`
  `test_m12_no_fixture_can_be_forced_to_auto_approve` - forced APPROVE/1.0-confidence
  recommendation still yields `human_review`; `tests/integration/test_decision_service.py:166`
  proves the same through real HTTP)
- [x] Forced approve recommendation above limit fails (same tests as above, plus proven live
  against the real, non-mocked Groq LLM by `scripts/verify_phase8.py`'s anti-cheese journey -
  INV-1013, over-ceiling + prompt-injection `notes`, route asserted `human_review`)


---

# 2. Must-Have Technical Requirements

## Repository

### M1 — Private monorepo

- [x] Single GitHub repository (`github.com/miryamizadka/invoice-approval-workflow`, single
  `origin` remote)
- [x] Everything needed exists in repository (single `Dockerfile` + `docker-compose.yml` +
  `pyproject.toml` - `docker compose up --build` works from the repo alone, no external
  dependency, proven throughout this project)


### M2 — GitHub Flow

- [x] main contains submission version (`main` is current through Notification Service +
  Intake fixes, `00d9200`; Phase 8 still on `feature/verification-suite`, not yet merged -
  in-progress work, not a gap)
- [x] Features developed in branches (13 `feature/*` branches - scaffolding, decision-agent,
  decision-service, intake-service, dapr*, approval-service, payment-service,
  notification-service, verification-suite - all merged into `main` via PR, `(#7)`...`(#12)`
  references in `git log --oneline main`)
- [x] No secrets committed (`.env` does not appear in `git ls-files` - verified directly, not
  assumed)
- [x] .gitignore exists
- [x] LICENSE exists
- [x] .env.example exists


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
- [x] Databases included (`postgres:16-alpine`, running - consumed directly by the Audit service's
  `audit_trail` table since F9, see ADR-008)
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
ETag optimistic concurrency (INV-1014), not just simple save/get.

- [x] Service invocation used (`services/intake/approval_status_client.py`'s
  `DaprApprovalStatusClient` - Intake calls Approval's `GET /approvals/{tracking_id}` through
  the Dapr sidecar's HTTP invoke API, enriching `GET /invoices/{tracking_id}` with live approval
  status for `human_review` submissions. The one synchronous cross-service call in the system -
  every other flow is deliberately event-driven; see ARCHITECTURE.md §7. Verified live:
  `approval.status` transitions `pending` → `approved` as the approver acts while Intake's own
  `decision` stays frozen; stopping Approval Service mid-flow still returns 200 with
  `approval: null`, never a 500; `verify_phase8` unaffected)
- [x] Pub/Sub used (`invoice.submitted` published by Intake via `DaprDecisionPublisher`;
  `decision.completed` published by Decision via `DaprDecisionOutcomePublisher`; `approval.completed`
  published by Approval via `DaprApprovalOutcomePublisher`; `payment.completed` published by
  Payment via `DaprPaymentOutcomePublisher`; all subscribed to via `dapr-ext-fastapi`'s `DaprApp`)
- [x] Dapr state used (`DaprStateInvoiceRepository` for Intake, `DaprStateApprovalRepository` for
  Approval, `DaprStatePaymentRepository`/`DaprStateBudgetRepository` for Payment - the same
  append-only-index pattern for records; budgets additionally use ETag-based optimistic
  concurrency, backed by the `statestore` component)
- [x] Dapr secrets used (`services/decision/service/dapr_secret_loader.py` - `GROQ_API_KEY`
  fetched via Dapr's Secrets API, `dapr/components/secretstore.yaml`'s `secretstores.local.env`,
  instead of `os.environ` directly, mirroring F7/M13's config-loader shape exactly (I/O vs pure
  split: `load_groq_api_key()` never raises, `resolve_provider_from_secret()` is pure and fully
  unit tested). Fetch-once at startup, same posture as thresholds/policy - the provider is built
  synchronously first [fail-fast, unchanged], then optionally rebuilt from the secret once Dapr's
  async lifespan hook resolves. Verified live: `docker compose logs decision` shows
  `llm_provider_rebuilt_from_dapr_secret`; with `decision-dapr` unreachable, the service still
  starts and serves correctly on the env-var fallback [warning logged, not a crash];
  `verify_phase8` including the real-LLM INV-1013 anti-cheese test passes against the
  secret-sourced provider)


## M6 — API Gateway

- [x] Single external entry point (Traefik, `traefik/dynamic.yml` file provider - see ADR-007
  for why not the Docker provider; PLAN.md Phase 8.5 for full verification. Intake, Approval,
  Payment, Notification, and Audit (added in F9) all lose their direct host port, reachable only
  via `localhost:8080`; Decision has neither a port nor a gateway route at all, pure choreography)
- [x] Rate limiting implemented (shared per-client-IP middleware, `average=10`/`burst=50` -
  proven live with genuine concurrent load [183/300 requests hit 429], and proven **not** to
  false-positive against real usage [`verify_phase8.py`'s full run through the gateway
  completed with zero 429s])


## M7 — Minimal UI

- [x] Submit invoice (`services/ui/static/index.html` form -> `POST /invoices` through the
  gateway's `/ui` + `/invoices` routes; static HTML/CSS/vanilla JS, no framework - see ADR-010)
- [x] View status (polls `GET /invoices/{tracking_id}` every 2s, stops on terminal status)
- [x] View decision (route + plain-language reason rendered once available; errors mapped to
  plain language, not raw fetch failures)
- [x] Approver escalation queue + actions (`services/ui/static/approvals.html` - beyond M7's
  literal 3 bullets, but committed to explicitly by `ARCHITECTURE.md` §4's "UI (M7)" description;
  approve/reject/request-info, polls every 5s)
- [x] F5 fully closed via this UI (see F5 above) - submitter can see `waiting_info` and respond
- [x] `verify_phase8` still passes after the UI + F5 additions (no regression)


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
  unit tests for the RESERVED-resume path (success and failure outcomes). The resumed charge
  step itself is also idempotent: `PaymentGateway.charge()` takes `idempotency_key=tracking_id`
  (`SimulatedPaymentGateway`/`FakePaymentGateway` cache the outcome per key) - previously this was
  only accidentally safe because both gateways are pure/stateless; now it's a real guarantee, not
  a coincidence. Verified via a dedicated test (`_execute_charge` invoked twice for the same
  still-RESERVED record → gateway's underlying charge logic runs exactly once) and live via
  `docker compose` (INV-1012's payment-failure/compensation journey re-run, unaffected). See
  `docs/adr/ADR-004-Saga-Orchestration.md` for the full reasoning, including the one known,
  accepted limitation this does *not* close (no business/technical failure distinction at the
  gateway - there is no real payment processor to model a transient failure against).
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

- [x] Policy configurable (Dapr `configuration.redis` store, fetch-once at Decision startup -
  see F7 above and ADR-009)
- [x] Threshold configurable (same mechanism, all 8 `AutonomyThresholds` fields independently
  overridable; per-field validation - an invalid value falls back to the default for that field
  only, logged as a warning, doesn't invalidate the rest)
- [x] No hardcoded limits (`DEFAULT_THRESHOLDS`/`policy.md` remain only as the resilience
  fallback if the store is empty/unreachable - the runtime source of truth is the config store
  whenever it has a value; a real client-construction failure mode was found and fixed during
  live verification - see ADR-009's "Resilience" section - the service now provably starts on
  defaults rather than crashing when Dapr's sidecar isn't ready)


---

# Cross Cutting

## M14 — Observability

- [x] Structured logs (`services/decision/service/logging_config.py`, JSON, stdlib-only)
- [x] Correlation id everywhere (all five services - Intake, Decision, Approval, Payment,
  Notification - include `correlation_id` in their structured logs; verified directly via
  `grep -rl correlation_id services/{intake,decision,approval,payment,notification}/`,
  10/14/4/8/8 files respectively - not just Decision Service)

## M15 — Code Quality

- [x] Clean architecture (the same Manager/Engine/Accessor/Resource layering - thin `app.py` +
  transport-agnostic `service.py` + `repository.py`/`dapr_state_repository.py` - repeats
  consistently across all five services, not just Decision; verified directly by reviewing
  each service's directory structure)
- [x] Error handling (`AgentError` -> documented `human_review` fallback, never a crash; a
  global exception handler for genuinely unexpected errors, logged with correlation_id)
- [x] Health checks (`GET /health`, deliberately no LLM connectivity check - see decision plan)
- [x] Separation of concerns (`Decider.decide()` has zero FastAPI/HTTP imports; `build_decider()`
  is the composition seam a future Dapr transport reuses without touching the HTTP layer)
- [x] LLM provider abstraction (`LLMProvider` Protocol, swappable via `LLM_PROVIDER` env var)
- [x] Provider failure handling (fail-fast on missing key; SDK/empty-completion errors wrapped
  in `LLMProviderError`, never silent)


## M16 — CI

- [x] CI pipeline exists (`.github/workflows/ci.yml`; `quality` + `docker-build` jobs, verified
  green on GitHub Actions - run [29174857998](https://github.com/miryamizadka/invoice-approval-workflow/actions/runs/29174857998))
- [x] Runs on push (`on: push` / `pull_request` / `workflow_dispatch`)
- [x] Quality gates (Ruff, MyPy, pytest+coverage all block the `quality` job on failure;
  `docker-build` proves the Dockerfile stays buildable)


## M17 — Automated Tests

- [x] Tests run in CI (`pytest --cov=services --cov=shared`, 416 tests, all green on GitHub
  Actions; `LLM_PROVIDER=mock` explicitly set - confirmed in the run logs that `GROQ_API_KEY`
  is never set and no `api.groq.com` call occurs, per ARCHITECTURE.md §14's "LLM stubbed in CI")
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

- [x] GitHub Flow followed (feature branch + PR + merge for every milestone, PR #7-#19)
- [x] Clean repository (all merged feature branches deleted locally and on origin - verified
  via `git branch -a`: only `main` remains)


## D4 — API Documentation

- [x] OpenAPI available (auto-generated by FastAPI from the existing Pydantic models, at
  `/openapi.json`)
- [x] Swagger / Scalar available (FastAPI's default Swagger UI at `/docs`, no extra config)


## D5 — Verification

CRITICAL

- [x] Single command runs verification (`python -m scripts.verify_phase8`, run live twice in a
  row without resetting the environment - see PLAN.md Phase 8 "Verification")
- [x] Auto approve scenario passes (INV-1001 + INV-1002, two distinct fixtures)
- [x] Human escalation passes (INV-1003 - escalate, approve, pay)
- [x] Duplicate scenario passes (INV-1007 - F3 proven live, never reaches Payment)
- [x] Payment failure compensation passes (INV-1012, now also part of the single verification
  command, not just the earlier manual PLAN.md Phase 6 run)
- [x] Anti-cheese test passes (INV-1013, against the real Groq LLM, no mock)


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

Implemented (`publish` job, `.github/workflows/ci.yml`, `needs: [quality, docker-build]`,
runs only on push to `main`) - builds and pushes to `ghcr.io/miryamizadka/
invoice-approval-workflow` (`latest` + commit SHA), via the existing `GITHUB_TOKEN`, no new
secret. **Checkbox stays unchecked until live-verified after a real merge to `main`** - not
yet confirmed that `publish` actually runs (vs. skips) and that the pushed image is pullable.

- [ ] Automatic artifact publishing (implemented, pending live verification after merge)


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

Already fully covered - not new work, just an unchecked box. Verified directly
(not assumed): `pytest --collect-only -q` → 451 tests collected.

- [x] Unit tests (`tests/unit/` - one suite per service)
- [x] Integration tests (`tests/integration/` - HTTP + TestClient, one suite per service)
- [x] End-to-end tests (`scripts/verify_phase8.py`, already checked off under D5 - runs
  against a real `docker compose` stack: Dapr sidecars, Redis, Postgres, Traefik, and the
  real Groq LLM for INV-1013, through the actual gateway, no mocks)


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