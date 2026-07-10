# ADR-008: Audit Trail via a Dedicated Audit Service with Direct Postgres Access

**Status:** Accepted

## Context

F9 ("Complete decision trail", MASTER_CHECKLIST.md, Priority: MUST HAVE) requires that every invoice's full journey - correlation id, invoice extraction, rules applied, agent recommendation, final decision, payment outcome - be stored and retrievable as one record.

Direct inspection of the three business services' own domain models confirmed F9 is not satisfied today: `recommendation` is persisted only by Approval's `PendingApproval`, and only for items escalated to `human_review`. For `auto_approve`/`reject`/`duplicate` routes - the majority of invoices - the agent's recommendation and reasoning are never persisted anywhere after the request completes. There is also no single queryable source: each service keeps its own fragment of the journey in its own Dapr state store, keyed independently, so reconstructing "the complete trail" for a given tracking_id today means querying up to three different services' endpoints and stitching the results together by hand.

F8 (Dashboard, Priority: NICE TO HAVE) asks for aggregate/historical metrics - auto-approval rate, money auto-approved vs. human-approved. These are inherently cross-invoice aggregations, not point-in-time state, and depend on F9's data existing in queryable form.

Postgres (`docker-compose.yml`'s `postgres` service) has existed since the M4 phase, unused - reserved for a role `ARCHITECTURE.md` §8 already anticipated ("Decisions | PostgreSQL").

## Decision

Add a new `audit` service (IDesign-layered, same shape as Notification: `app.py` transport → `service.py` Manager → `repository.py` Protocol → `postgres_repository.py` Accessor) that subscribes to the three existing choreography events - `decision.completed`, `approval.completed`, `payment.completed` - and projects them into one row per `tracking_id` in a Postgres `audit_trail` table. `GET /audit/{tracking_id}` exposes the assembled trail, routed through the M6 gateway like every other externally-reachable endpoint.

Audit is a **read model / projection, not a source of truth**: each business service (Intake/Decision/Approval/Payment) keeps owning its own operational state. Audit builds a cross-service history for querying and reporting only, and does not replace logging - it captures business events (what was decided), not operational logs (what happened in the process).

No new event is introduced: `decision.completed`, `approval.completed`, and `payment.completed` each already carry a full `invoice` + `decision` copy (`shared/contracts/models.py`), so any of the three can independently create the row if it is the first to arrive for a given `tracking_id` - the design does not assume `decision.completed` always arrives first, even though in practice it almost always does.

Audit is observational only: it never blocks or influences approval/payment decisions and publishes nothing back. A failure of the Audit service must never prevent Approval/Payment/Notification from completing - this is not new engineering, it falls out of Dapr pub/sub's existing fan-out model (each subscriber to a topic is already independent of every other subscriber); Audit simply adds one more independent consumer. Verified live by stopping `audit`/`audit-dapr` and confirming a full invoice journey still completes normally end to end (see PLAN.md's Phase 9 verification).

The `audit_trail` table is monotonically enriched, not a literal immutable event ledger: there is one row per `tracking_id`, and each handler writes only the columns its own event carries, never touching a column a different handler already populated. Rewriting the shared invoice/decision-derived columns on a later event is idempotent (all three events carry identical copies for the same `tracking_id`), not a real overwrite.

## Provider mechanism: direct `asyncpg` access, not Dapr state store

Every other service in this project accesses its store exclusively through Dapr's state API - a key→blob abstraction, good for point lookups, not built for historical querying, ordering, or cross-record aggregation. F8's stated requirements (rates, money totals) need real SQL (`SUM`/`COUNT`/`GROUP BY`), which a KV blob store cannot provide without pulling every row into application code and aggregating there - a design that does not scale.

**Audit accesses Postgres directly via `asyncpg`**, wrapped behind an `AuditRepository` Protocol exactly like every other service's Accessor layer - `AuditService` has no idea it isn't talking to Dapr state. This is a deliberate, isolated exception to "every service goes through Dapr", justified by a genuinely different requirement (query/aggregation, not point lookup), and consistent with `ARCHITECTURE.md` §8's own pre-existing intent for Postgres to hold relational data, not a Dapr-state blob store layered on top of it.

Schema is created via a plain `CREATE TABLE IF NOT EXISTS` at service startup (`ensure_schema()`, called from the FastAPI lifespan hook) - no migration framework, matching the project's existing minimal-tooling posture (e.g. Payment's own startup budget-seeding).

## Consequences

**Pros:**
- F9 is genuinely satisfied: every invoice's recommendation, rules, and decision are stored regardless of route, in one place, keyed by `tracking_id`.
- Gives the previously-unused Postgres container its intended role.
- F8, if built later, reads from this one table - no fan-out across Intake/Approval/Payment/Notification needed.
- Real SQL aggregation is available (`GROUP BY route`, `SUM(total)`, etc.) without pulling data into application code.

**Cons:**
- One more service/sidecar pair in the stack.
- One inconsistency in access pattern versus the rest of the codebase (direct SQL vs. Dapr state) - documented here specifically so it isn't mistaken for an oversight.
- No migration framework means schema evolution is manual (acceptable at this project's scale; would need revisiting if the schema grows significantly).

## Non-goals

This phase is trail storage + point lookup only: no dashboard, no analytics/BI, no search, no filtering, no pagination, no export. `GET /audit/{tracking_id}` is the entire read surface. If F8 (Dashboard) is ever built, **it should query the Audit service's API, not Postgres directly** - going straight to `audit_trail` from another service would bypass the service boundary this ADR establishes and couple a second service to the table's exact schema.

## Forward-looking notes

- **Schema is append-friendly.** Every event-specific column (`recommendation_*`, `approval_*`, `payment_*`) is nullable by design, and the evolving nested shapes (`invoice_json`, `triggered_rules`, `recommendation_cited_rules`) are JSONB, not flattened columns - a new field added to `Invoice`/`Decision`/`Recommendation` upstream shows up in the JSON automatically, no migration needed. Nothing here assumes today's event shapes are final.
- **`recommendation_reasoning` stores the agent's full text, not a truncated summary** - a deliberate choice, not an oversight. Postgres `TEXT` is unbounded (unlike `VARCHAR(n)`), so there's no schema-level size limit; today's reasoning text is short (a paragraph). If the LLM prompt ever changes to produce much longer output, this is worth revisiting, but isn't a concern at the current scope.

## Alternatives considered

- **Extend Dapr state store to also cover Audit** (a new `state.postgresql` Dapr component, same `AuditRepository` Protocol pattern as every other service) - rejected. Keeps 100% consistency with "everything through Dapr", but the underlying storage would still be Dapr's generic key→JSONB-blob schema, not real relational columns - F8's aggregation needs would still require pulling every row into Python and summing there, gaining none of the reason Postgres was chosen.
- **Extend an existing service's state instead of a new Audit service** - rejected. No existing service naturally owns "the whole cross-service trail"; forcing it into e.g. Payment or Notification would blur IDesign's single-responsibility boundaries and couple an unrelated service's lifecycle to Audit's.
- **Also subscribe to `invoice.submitted`** - rejected. `decision.completed` already carries the full invoice for every case, including Intake's duplicate short-circuit path (`services/intake/decision_completed_publisher.py`), so a fourth subscription would add no information, only complexity.
