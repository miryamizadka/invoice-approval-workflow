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

    await gateway.charge(clean_invoice(id="INV-1"))  # does not raise


async def test_fake_gateway_raises_for_configured_ids() -> None:
    gateway = FakePaymentGateway(fail_for={"INV-1012"})

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"))


async def test_fake_gateway_records_charged_ids() -> None:
    gateway = FakePaymentGateway()

    await gateway.charge(clean_invoice(id="INV-1"))

    assert gateway.charged == ["INV-1"]


async def test_fake_gateway_does_not_record_failed_charges() -> None:
    gateway = FakePaymentGateway(fail_for={"INV-1012"})

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"))

    assert gateway.charged == []


# --- SimulatedPaymentGateway --------------------------------------------


async def test_simulated_gateway_succeeds_when_id_not_configured() -> None:
    gateway = SimulatedPaymentGateway(failure_ids="INV-1012")

    await gateway.charge(clean_invoice(id="INV-1"))  # does not raise


async def test_simulated_gateway_raises_for_configured_id() -> None:
    gateway = SimulatedPaymentGateway(failure_ids="INV-1012")

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"))


async def test_simulated_gateway_reads_env_var_when_not_passed_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAYMENT_SIMULATE_FAILURE_IDS", "INV-1012")
    gateway = SimulatedPaymentGateway()

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"))


async def test_simulated_gateway_parses_comma_separated_ids_and_trims_whitespace() -> None:
    gateway = SimulatedPaymentGateway(failure_ids=" INV-1012 , INV-2000")

    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-1012"))
    with pytest.raises(PaymentGatewayError):
        await gateway.charge(clean_invoice(id="INV-2000"))
    await gateway.charge(clean_invoice(id="INV-3000"))  # does not raise


async def test_simulated_gateway_defaults_to_no_failures_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PAYMENT_SIMULATE_FAILURE_IDS", raising=False)
    gateway = SimulatedPaymentGateway()

    await gateway.charge(clean_invoice(id="INV-1012"))  # does not raise


# --- Protocol conformance -------------------------------------------------


@pytest.mark.parametrize("gateway", [FakePaymentGateway(), SimulatedPaymentGateway(failure_ids="")])
def test_every_gateway_satisfies_payment_gateway_protocol(gateway: PaymentGateway) -> None:
    assert isinstance(gateway, PaymentGateway)
