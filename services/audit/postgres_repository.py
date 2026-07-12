"""Postgres-backed AuditRepository - the one deliberate exception to "every
service accesses its store through Dapr" (see ADR-008). F9's future
aggregate queries (F8: SUM/COUNT/GROUP BY) need real SQL, not a KV blob
store, so this talks to Postgres directly via asyncpg - wrapped behind the
same AuditRepository Protocol every other Accessor uses, so AuditService
never knows this isn't Dapr state.

Connection settings come from POSTGRES_HOST/PORT/USER/PASSWORD/DB env vars
(same os.environ.get(..., default) convention as
services/decision/accessors/factory.py - no central Settings class),
defaulting to the values already set on the `postgres` docker-compose
service.

ensure_schema() runs a plain `CREATE TABLE IF NOT EXISTS` at app startup
(see app.py's lifespan hook) - no migration framework, same minimal-tooling
posture as Payment's startup budget-seeding.

Every upsert_* statement writes the full set of base columns (invoice/
decision-derived) plus its own event-specific columns, and nothing else -
mirroring InMemoryAuditRepository's _base_fields()+_merge() split. This is
what makes each handler order-independent: any of the three events can
INSERT the row from scratch, and re-writing the (identical) base columns on
a later event is idempotent, never a real overwrite.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import asyncpg

from services.audit.models import AuditTrail
from services.audit.repository import RouteSummaryBucket
from shared.contracts.models import (
    ApprovalCompletedEvent,
    Decision,
    DecisionCompletedEvent,
    Invoice,
    PaymentCompletedEvent,
)


class AuditRepositoryError(Exception):
    """Raised on any Postgres failure - never caught silently, same
    fail-clean posture as NotificationRepositoryError/PaymentRepositoryError.
    Propagates uncaught out of the subscription handler so Dapr redelivers."""


def _dsn() -> str:
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    user = os.environ.get("POSTGRES_USER", "approvalflow")
    password = os.environ.get("POSTGRES_PASSWORD", "approvalflow")
    database = os.environ.get("POSTGRES_DB", "approvalflow")
    return f"postgres://{user}:{password}@{host}:{port}/{database}"


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS audit_trail (
    tracking_id TEXT PRIMARY KEY,
    invoice_number TEXT NOT NULL,
    vendor TEXT NOT NULL,
    department TEXT NOT NULL,
    category TEXT NOT NULL,
    total NUMERIC NOT NULL,
    currency TEXT NOT NULL,
    invoice_json JSONB NOT NULL,
    route TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    triggered_rules JSONB NOT NULL,
    recommendation_type TEXT,
    recommendation_confidence DOUBLE PRECISION,
    recommendation_cited_rules JSONB,
    recommendation_reasoning TEXT,
    decision_completed_at TIMESTAMPTZ,
    approval_resolution TEXT,
    approval_completed_at TIMESTAMPTZ,
    payment_resolution TEXT,
    payment_reason TEXT,
    payment_completed_at TIMESTAMPTZ
)
"""

_BASE_COLUMNS = (
    "tracking_id, invoice_number, vendor, department, category, total, "
    "currency, invoice_json, route, decision_reason, triggered_rules"
)
_BASE_PLACEHOLDERS = "$1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11::jsonb"
_BASE_SET = """
    invoice_number = EXCLUDED.invoice_number,
    vendor = EXCLUDED.vendor,
    department = EXCLUDED.department,
    category = EXCLUDED.category,
    total = EXCLUDED.total,
    currency = EXCLUDED.currency,
    invoice_json = EXCLUDED.invoice_json,
    route = EXCLUDED.route,
    decision_reason = EXCLUDED.decision_reason,
    triggered_rules = EXCLUDED.triggered_rules
"""

_UPSERT_DECISION_SQL = f"""
INSERT INTO audit_trail (
    {_BASE_COLUMNS}, recommendation_type, recommendation_confidence,
    recommendation_cited_rules, recommendation_reasoning, decision_completed_at
) VALUES (
    {_BASE_PLACEHOLDERS}, $12, $13, $14::jsonb, $15, $16
)
ON CONFLICT (tracking_id) DO UPDATE SET
    {_BASE_SET},
    recommendation_type = EXCLUDED.recommendation_type,
    recommendation_confidence = EXCLUDED.recommendation_confidence,
    recommendation_cited_rules = EXCLUDED.recommendation_cited_rules,
    recommendation_reasoning = EXCLUDED.recommendation_reasoning,
    decision_completed_at = EXCLUDED.decision_completed_at
"""

_UPSERT_APPROVAL_SQL = f"""
INSERT INTO audit_trail (
    {_BASE_COLUMNS}, approval_resolution, approval_completed_at
) VALUES (
    {_BASE_PLACEHOLDERS}, $12, $13
)
ON CONFLICT (tracking_id) DO UPDATE SET
    {_BASE_SET},
    approval_resolution = EXCLUDED.approval_resolution,
    approval_completed_at = EXCLUDED.approval_completed_at
"""

_UPSERT_PAYMENT_SQL = f"""
INSERT INTO audit_trail (
    {_BASE_COLUMNS}, payment_resolution, payment_reason, payment_completed_at
) VALUES (
    {_BASE_PLACEHOLDERS}, $12, $13, $14
)
ON CONFLICT (tracking_id) DO UPDATE SET
    {_BASE_SET},
    payment_resolution = EXCLUDED.payment_resolution,
    payment_reason = EXCLUDED.payment_reason,
    payment_completed_at = EXCLUDED.payment_completed_at
"""

_SELECT_SQL = "SELECT * FROM audit_trail WHERE tracking_id = $1"

_ROUTE_SUMMARY_SQL = """
SELECT route, currency, approval_resolution, COUNT(*) AS n, SUM(total) AS total
FROM audit_trail
GROUP BY route, currency, approval_resolution
ORDER BY route, currency
"""


def _base_params(invoice: Invoice, decision: Decision) -> tuple[object, ...]:
    return (
        decision.correlation_id,
        invoice.invoice_number,
        invoice.vendor,
        invoice.department,
        invoice.category.value,
        invoice.total,
        invoice.currency,
        invoice.model_dump_json(),
        decision.route.value,
        decision.reason,
        json.dumps(decision.triggered_rules),
    )


class PostgresAuditRepository:
    def __init__(self, *, dsn: str | None = None) -> None:
        self._dsn = dsn or _dsn()
        self._pool: asyncpg.Pool | None = None

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn)
        return self._pool

    async def ensure_schema(self) -> None:
        pool = await self._get_pool()
        try:
            await pool.execute(_CREATE_TABLE_SQL)
        except asyncpg.PostgresError as exc:
            raise AuditRepositoryError(f"Failed to ensure audit_trail schema: {exc}") from exc

    async def upsert_decision(self, event: DecisionCompletedEvent) -> None:
        recommendation = event.recommendation
        params = (
            *_base_params(event.invoice, event.decision),
            recommendation.recommendation.value if recommendation else None,
            recommendation.confidence if recommendation else None,
            json.dumps(recommendation.cited_rules) if recommendation else None,
            recommendation.reasoning if recommendation else None,
            datetime.now(UTC),
        )
        await self._execute(_UPSERT_DECISION_SQL, params, "decision")

    async def upsert_approval(self, event: ApprovalCompletedEvent) -> None:
        params = (
            *_base_params(event.invoice, event.decision),
            event.resolution.value,
            datetime.now(UTC),
        )
        await self._execute(_UPSERT_APPROVAL_SQL, params, "approval")

    async def upsert_payment(self, event: PaymentCompletedEvent) -> None:
        params = (
            *_base_params(event.invoice, event.decision),
            event.resolution.value,
            event.reason,
            datetime.now(UTC),
        )
        await self._execute(_UPSERT_PAYMENT_SQL, params, "payment")

    async def get(self, tracking_id: str) -> AuditTrail | None:
        pool = await self._get_pool()
        try:
            row = await pool.fetchrow(_SELECT_SQL, tracking_id)
        except asyncpg.PostgresError as exc:
            raise AuditRepositoryError(f"Failed to read audit trail: {exc}") from exc
        if row is None:
            return None
        return AuditTrail(
            tracking_id=row["tracking_id"],
            invoice_number=row["invoice_number"],
            vendor=row["vendor"],
            department=row["department"],
            category=row["category"],
            total=row["total"],
            currency=row["currency"],
            invoice=json.loads(row["invoice_json"]),
            route=row["route"],
            decision_reason=row["decision_reason"],
            triggered_rules=json.loads(row["triggered_rules"]),
            recommendation_type=row["recommendation_type"],
            recommendation_confidence=row["recommendation_confidence"],
            recommendation_cited_rules=(
                json.loads(row["recommendation_cited_rules"])
                if row["recommendation_cited_rules"] is not None
                else None
            ),
            recommendation_reasoning=row["recommendation_reasoning"],
            decision_completed_at=row["decision_completed_at"],
            approval_resolution=row["approval_resolution"],
            approval_completed_at=row["approval_completed_at"],
            payment_resolution=row["payment_resolution"],
            payment_reason=row["payment_reason"],
            payment_completed_at=row["payment_completed_at"],
        )

    async def get_route_summary(self) -> list[RouteSummaryBucket]:
        pool = await self._get_pool()
        try:
            rows = await pool.fetch(_ROUTE_SUMMARY_SQL)
        except asyncpg.PostgresError as exc:
            raise AuditRepositoryError(f"Failed to compute route summary: {exc}") from exc
        return [
            RouteSummaryBucket(
                route=row["route"],
                currency=row["currency"],
                approval_resolution=row["approval_resolution"],
                count=row["n"],
                total=row["total"],
            )
            for row in rows
        ]

    async def _execute(self, sql: str, params: tuple[object, ...], kind: str) -> None:
        pool = await self._get_pool()
        try:
            await pool.execute(sql, *params)
        except asyncpg.PostgresError as exc:
            raise AuditRepositoryError(f"Failed to upsert {kind} audit row: {exc}") from exc
