"""Tests for PaymentGateway implementations - FakePaymentGateway (test
double) and SimulatedPaymentGateway (the real, only production
implementation - there is no real payment processor in this project)."""

from __future__ import annotations

import pytest

from services.payment.accessors.fake_gateway import FakePaymentGateway
from services.payment.accessors.payment_gateway import PaymentGateway, PaymentGatewayError
from services.payment.accessors.simulated_gateway import SimulatedPaymentGateway
from tests.support.decision_fixtures import clean_invoice

# --- FakePaymentGateway ------------------------------------------------


async def test_fake_gateway_succeeds_by_default() -> None:
    gateway = FakePaymentGateway()

    await gateway.charge(clean_invoice(id="INV-1"), idempotency_key="k1")  # does not raise


async def test_fake_gateway_raises_for_configured_ids() -> None:
    gateway = FakePaymentGateway(fail_for={"INV-1012"})

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"), idempotency_key="k1")


async def test_fake_gateway_records_charged_ids() -> None:
    gateway = FakePaymentGateway()

    await gateway.charge(clean_invoice(id="INV-1"), idempotency_key="k1")

    assert gateway.charged == ["INV-1"]


async def test_fake_gateway_does_not_record_failed_charges() -> None:
    gateway = FakePaymentGateway(fail_for={"INV-1012"})

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"), idempotency_key="k1")

    assert gateway.charged == []


# --- SimulatedPaymentGateway --------------------------------------------


async def test_simulated_gateway_succeeds_when_id_not_configured() -> None:
    gateway = SimulatedPaymentGateway(failure_ids="INV-1012")

    await gateway.charge(clean_invoice(id="INV-1"), idempotency_key="k1")  # does not raise


async def test_simulated_gateway_raises_for_configured_id() -> None:
    gateway = SimulatedPaymentGateway(failure_ids="INV-1012")

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"), idempotency_key="k1")


async def test_simulated_gateway_reads_env_var_when_not_passed_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAYMENT_SIMULATE_FAILURE_IDS", "INV-1012")
    gateway = SimulatedPaymentGateway()

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"), idempotency_key="k1")


async def test_simulated_gateway_parses_comma_separated_ids_and_trims_whitespace() -> None:
    gateway = SimulatedPaymentGateway(failure_ids=" INV-1012 , INV-2000")

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"), idempotency_key="k1")
    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-2000"), idempotency_key="k2")
    await gateway.charge(clean_invoice(id="INV-3000"), idempotency_key="k3")  # does not raise


async def test_simulated_gateway_defaults_to_no_failures_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PAYMENT_SIMULATE_FAILURE_IDS", raising=False)
    gateway = SimulatedPaymentGateway()

    await gateway.charge(clean_invoice(id="INV-1012"), idempotency_key="k1")  # does not raise


# --- Protocol conformance -------------------------------------------------


@pytest.mark.parametrize("gateway", [FakePaymentGateway(), SimulatedPaymentGateway(failure_ids="")])
def test_every_gateway_satisfies_payment_gateway_protocol(gateway: PaymentGateway) -> None:
    assert isinstance(gateway, PaymentGateway)


# --- idempotency_key: replay without re-executing (M10) ---------------------
#
# Each gateway gains an `execution_count` counter that only increments the
# first time a given idempotency_key is seen - proving a second charge() call
# with the same key replays the cached outcome instead of re-running the
# underlying charge logic. This is what makes PaymentService's documented
# crash-recovery resume (models.py::PaymentRecord - redelivery resumes at the
# charge step, not just the reserve step) safe against a real, stateful
# gateway, not just this project's pure simulated ones.


async def test_fake_gateway_replays_cached_success_without_re_executing() -> None:
    gateway = FakePaymentGateway()
    invoice = clean_invoice(id="INV-1")

    await gateway.charge(invoice, idempotency_key="same-key")
    await gateway.charge(invoice, idempotency_key="same-key")

    assert gateway.execution_count == 1
    assert gateway.charged == ["INV-1"]  # not appended twice


async def test_fake_gateway_replays_cached_failure_without_re_executing() -> None:
    gateway = FakePaymentGateway(fail_for={"INV-1012"})
    invoice = clean_invoice(id="INV-1012")

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(invoice, idempotency_key="same-key")
    with pytest.raises(PaymentGatewayError):
        await gateway.charge(invoice, idempotency_key="same-key")

    assert gateway.execution_count == 1


async def test_fake_gateway_different_keys_execute_independently() -> None:
    gateway = FakePaymentGateway()
    invoice = clean_invoice(id="INV-1")

    await gateway.charge(invoice, idempotency_key="key-a")
    await gateway.charge(invoice, idempotency_key="key-b")

    assert gateway.execution_count == 2
    assert gateway.charged == ["INV-1", "INV-1"]


async def test_simulated_gateway_replays_cached_success_without_re_executing() -> None:
    gateway = SimulatedPaymentGateway(failure_ids="INV-1012")
    invoice = clean_invoice(id="INV-1")

    await gateway.charge(invoice, idempotency_key="same-key")
    await gateway.charge(invoice, idempotency_key="same-key")

    assert gateway.execution_count == 1


async def test_simulated_gateway_replays_cached_failure_without_re_executing() -> None:
    gateway = SimulatedPaymentGateway(failure_ids="INV-1012")
    invoice = clean_invoice(id="INV-1012")

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(invoice, idempotency_key="same-key")
    with pytest.raises(PaymentGatewayError):
        await gateway.charge(invoice, idempotency_key="same-key")

    assert gateway.execution_count == 1


async def test_simulated_gateway_different_keys_execute_independently() -> None:
    gateway = SimulatedPaymentGateway(failure_ids="")
    invoice = clean_invoice(id="INV-1")

    await gateway.charge(invoice, idempotency_key="key-a")
    await gateway.charge(invoice, idempotency_key="key-b")

    assert gateway.execution_count == 2
