"""Integration tests for the Payment Service HTTP API.

Exercises the full chain: HTTP/subscription -> FastAPI -> PaymentService -
with InMemory repositories/a stub publisher/a fake gateway, no real Dapr
(same pattern as test_approval_service.py).

IMPORTANT: Payment is the first service with genuine async startup work
(budget seeding via the lifespan hook). Starlette's TestClient only runs
ASGI lifespan startup inside its `__enter__` (confirmed by reading the
installed starlette==1.3.1 source) - a bare `TestClient(app)` without
`with`, the pattern every other integration test file in this repo uses,
never runs it. Every test below therefore uses `with TestClient(app) as
client:` deliberately - this is NOT an oversight to "fix back" to match
the other files.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from services.payment.accessors.fake_gateway import FakePaymentGateway
from services.payment.app import create_app
from services.payment.repository import InMemoryBudgetRepository, InMemoryPaymentRepository
from shared.auth import AuthenticatedUser, Role, get_current_user
from shared.contracts.models import ApprovalCompletedEvent, ApprovalResolution, Decision, Route
from tests.support.decision_fixtures import RAW_FIXTURES, clean_invoice, invoice_from_fixture

_DEPARTMENT = "engineering-2026Q2"
_BUDGET_TOTAL = Decimal("50000.00")


class _StubOutcomePublisher:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


def _decision(route: Route = Route.AUTO_APPROVE, correlation_id: str = "corr-1") -> dict[str, Any]:
    return Decision(
        route=route, reason="test reason", triggered_rules=[], correlation_id=correlation_id
    ).model_dump(mode="json")


def _decision_completed_event_body(
    route: Route = Route.AUTO_APPROVE, correlation_id: str = "corr-1", total: str = "50.00"
) -> dict[str, Any]:
    return {
        "invoice": clean_invoice(total=Decimal(total)).model_dump(mode="json"),
        "decision": _decision(route, correlation_id),
        "recommendation": None,
    }


def _approval_completed_event_body(
    resolution: ApprovalResolution = ApprovalResolution.APPROVED,
    correlation_id: str = "corr-1",
    total: str = "50.00",
) -> dict[str, Any]:
    return {
        "invoice": clean_invoice(total=Decimal(total)).model_dump(mode="json"),
        "decision": _decision(Route.HUMAN_REVIEW, correlation_id),
        "resolution": resolution.value,
    }


def _post_decision_completed(client: TestClient, **overrides: Any) -> Any:
    return client.post(
        "/events/decision-completed", json={"data": _decision_completed_event_body(**overrides)}
    )


def _post_approval_completed(client: TestClient, **overrides: Any) -> Any:
    return client.post(
        "/events/approval-completed", json={"data": _approval_completed_event_body(**overrides)}
    )


def _app(
    repository: InMemoryPaymentRepository | None = None,
    budget_repository: InMemoryBudgetRepository | None = None,
    publisher: _StubOutcomePublisher | None = None,
    gateway: FakePaymentGateway | None = None,
) -> tuple[
    Any,
    InMemoryPaymentRepository,
    InMemoryBudgetRepository,
    _StubOutcomePublisher,
    FakePaymentGateway,
]:
    resolved_repository = repository or InMemoryPaymentRepository()
    resolved_budget_repository = budget_repository or InMemoryBudgetRepository()
    resolved_publisher = publisher or _StubOutcomePublisher()
    resolved_gateway = gateway or FakePaymentGateway()
    app = create_app(
        repository=resolved_repository,
        budget_repository=resolved_budget_repository,
        publisher=resolved_publisher,
        gateway=resolved_gateway,
    )
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="test-admin@example.com", role=Role.ADMIN
    )
    return (
        app,
        resolved_repository,
        resolved_budget_repository,
        resolved_publisher,
        resolved_gateway,
    )


def test_health_check() -> None:
    app, *_ = _app()
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "payment-service"}


def test_budgets_are_seeded_at_startup_before_any_event_arrives() -> None:
    app, _, budget_repository, _, _ = _app()
    with TestClient(app) as client:
        response = client.get(f"/budgets/{_DEPARTMENT}")

    assert response.status_code == 200
    assert Decimal(response.json()["remaining"]) == _BUDGET_TOTAL


def test_plain_testclient_without_context_manager_does_not_seed_budgets() -> None:
    """Regression test locking in the documented starlette gotcha - not just
    documenting it in prose. If this ever starts passing, someone changed
    starlette's lifespan behavior (or this test) without noticing."""
    app, _, _, _, _ = _app()
    client = TestClient(app)  # deliberately NOT using `with`

    response = client.get(f"/budgets/{_DEPARTMENT}")

    assert response.status_code == 404


def test_decision_completed_auto_approve_reserves_and_completes() -> None:
    app, repository, budget_repository, publisher, gateway = _app()
    with TestClient(app) as client:
        response = _post_decision_completed(client)

        assert response.status_code == 200
        payment = client.get("/payments/corr-1").json()
        assert payment["status"] == "completed"
        budget = client.get(f"/budgets/{_DEPARTMENT}").json()
        assert Decimal(budget["remaining"]) == _BUDGET_TOTAL - Decimal("50.00")
    assert len(publisher.published) == 1


def test_decision_completed_non_auto_approve_does_not_reach_payments() -> None:
    app, *_ = _app()
    with TestClient(app) as client:
        _post_decision_completed(client, route=Route.HUMAN_REVIEW)

        response = client.get("/payments/corr-1")

    assert response.status_code == 404


def test_approval_completed_approved_reserves_and_completes() -> None:
    app, *_ = _app()
    with TestClient(app) as client:
        _post_approval_completed(client)

        payment = client.get("/payments/corr-1").json()

    assert payment["status"] == "completed"


def test_approval_completed_rejected_does_not_reach_payments() -> None:
    app, *_ = _app()
    with TestClient(app) as client:
        _post_approval_completed(client, resolution=ApprovalResolution.REJECTED)

        response = client.get("/payments/corr-1")

    assert response.status_code == 404


def test_get_single_payment_returns_404_when_unknown() -> None:
    app, *_ = _app()
    with TestClient(app) as client:
        response = client.get("/payments/missing")

    assert response.status_code == 404


def test_list_payments_returns_all_records() -> None:
    app, *_ = _app()
    with TestClient(app) as client:
        _post_decision_completed(client, correlation_id="corr-1")
        _post_decision_completed(client, correlation_id="corr-2")

        response = client.get("/payments")

    assert response.status_code == 200
    assert {p["tracking_id"] for p in response.json()} == {"corr-1", "corr-2"}


def test_gateway_failure_produces_failed_status_and_releases_budget() -> None:
    gateway = FakePaymentGateway(fail_for={"TEST-0000"})
    app, *_ = _app(gateway=gateway)
    with TestClient(app) as client:
        _post_decision_completed(client)

        payment = client.get("/payments/corr-1").json()
        budget = client.get(f"/budgets/{_DEPARTMENT}").json()

    assert payment["status"] == "failed"
    assert Decimal(budget["remaining"]) == _BUDGET_TOTAL


def test_redelivery_after_completion_does_not_double_charge_or_publish() -> None:
    app, _, _, publisher, gateway = _app()
    with TestClient(app) as client:
        _post_decision_completed(client)
        _post_decision_completed(client)

    assert len(gateway.charged) == 1
    assert len(publisher.published) == 1


# --- required verification journeys: INV-1012, INV-1014A/B ------------------


def _raw_fixture(fixture_id: str) -> dict[str, Any]:
    return next(f for f in RAW_FIXTURES if f["id"] == fixture_id)


def test_inv_1012_gateway_failure_triggers_compensation_with_no_orphaned_reservation() -> None:
    invoice = invoice_from_fixture(_raw_fixture("INV-1012"))
    gateway = FakePaymentGateway(fail_for={"INV-1012"})
    app, *_ = _app(gateway=gateway)
    with TestClient(app) as client:
        baseline = Decimal(client.get(f"/budgets/{invoice.department}").json()["remaining"])

        response = client.post(
            "/events/approval-completed",
            json={
                "data": {
                    "invoice": invoice.model_dump(mode="json"),
                    "decision": _decision(Route.HUMAN_REVIEW, "inv-1012-corr"),
                    "resolution": ApprovalResolution.APPROVED.value,
                }
            },
        )
        assert response.status_code == 200

        payment = client.get("/payments/inv-1012-corr").json()
        after = Decimal(client.get(f"/budgets/{invoice.department}").json()["remaining"])

    assert payment["status"] == "failed"
    assert after == baseline  # no orphaned reservation


def test_inv_1014_concurrent_pair_exactly_one_succeeds_budget_never_negative() -> None:
    """asyncio.gather at the service layer directly, not two sequential
    TestClient HTTP calls - those don't truly overlap in time. This proves
    the saga's branching logic under real concurrency; the real Redis/ETag
    proof is the required live docker-compose run (scripts/
    verify_inv1014_concurrency.py), not this test."""
    invoice_a = invoice_from_fixture(_raw_fixture("INV-1014A"))
    invoice_b = invoice_from_fixture(_raw_fixture("INV-1014B"))
    app, _, budget_repository, _, _ = _app()
    payment_service = app.state.payment_service

    decision_a = Decision(
        route=Route.HUMAN_REVIEW, reason="test", triggered_rules=[], correlation_id="corr-a"
    )
    decision_b = Decision(
        route=Route.HUMAN_REVIEW, reason="test", triggered_rules=[], correlation_id="corr-b"
    )
    event_a = ApprovalCompletedEvent(
        invoice=invoice_a, decision=decision_a, resolution=ApprovalResolution.APPROVED
    )
    event_b = ApprovalCompletedEvent(
        invoice=invoice_b, decision=decision_b, resolution=ApprovalResolution.APPROVED
    )

    async def _run_and_check() -> None:
        await budget_repository.ensure_seeded(invoice_a.department, Decimal("1000.00"))
        await asyncio.gather(
            payment_service.handle_approval_completed(event_a),
            payment_service.handle_approval_completed(event_b),
        )
        record_a = await payment_service.get("corr-a")
        record_b = await payment_service.get("corr-b")
        statuses = {record_a.status.value, record_b.status.value}
        assert statuses == {"completed", "failed"}
        budget = await payment_service.get_budget(invoice_a.department)
        assert budget.remaining == Decimal("400.00")
        assert budget.remaining >= Decimal("0.00")

    asyncio.run(_run_and_check())


# --- N1: authentication/authorization gates ---------------------------------


def test_list_payments_requires_authentication() -> None:
    app, *_ = _app()
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        response = client.get("/payments")

    assert response.status_code == 401


def test_list_payments_requires_admin_role_not_submitter() -> None:
    app, *_ = _app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="submitter@example.com", role=Role.SUBMITTER
    )
    with TestClient(app) as client:
        response = client.get("/payments")

    assert response.status_code == 403


def test_get_budget_requires_admin_role_not_approver() -> None:
    app, *_ = _app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="approver@example.com", role=Role.APPROVER
    )
    with TestClient(app) as client:
        response = client.get(f"/budgets/{_DEPARTMENT}")

    assert response.status_code == 403


def test_get_single_payment_only_requires_authentication_not_admin_role() -> None:
    app, *_ = _app()
    with TestClient(app) as client:
        _post_decision_completed(client)
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            sub="submitter@example.com", role=Role.SUBMITTER
        )

        response = client.get("/payments/corr-1")

    assert response.status_code == 200
