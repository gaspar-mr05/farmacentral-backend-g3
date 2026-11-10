from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.clients.billing import (
    BillingConnectionError,
    BillingHTTPError,
    BillingSOAPFault,
    BillingTimeoutError,
)
from app.models import BillingAttempt, BillingTask, InvoiceSide, InvoiceStatus
from app.models import BillingOperationStatus as Status
from app.schemas.billing import BillingInvoice
from app.services.billing import BillingReconciliationService
from app.services.billing.payments import InvoicePaymentService, estimated_interest
from tests.services.test_billing_reconciliation import BillingSource
from tests.support.billing import make_invoice, make_period
from tests.support.billing_soap import invoice_values
from tests.support.farma_central import make_settings

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def official_invoice(*, invoice_id="invoice-1", issued_at=None, **overrides):
    return BillingInvoice.model_validate(
        invoice_values(
            **(
                {
                    "id": invoice_id,
                    "price": "100.25",
                    "interest": "0",
                    "totalPrice": "100.25",
                    "createdAt": (issued_at or NOW - timedelta(hours=1)).isoformat(),
                    "updatedAt": NOW.isoformat(),
                }
                | overrides
            )
        )
    )


class PaymentSource(BillingSource):
    def __init__(self, invoices):
        super().__init__(invoices)
        self.paid_ids = []
        self.pay_error = None

    async def pay_invoice(self, invoice_id):
        self.paid_ids.append(invoice_id)
        if self.pay_error:
            raise self.pay_error
        original = next(i for i in self.invoices if i.invoice_id == invoice_id)
        assert original.status == InvoiceStatus.PENDING
        paid = original.model_copy(update={"status": InvoiceStatus.PAID})
        self.invoices[self.invoices.index(original)] = paid
        return paid


def setup_payments(session, *, channel="fabric", supplier="1000", hours=1, extra=()):
    period = make_period(
        starts_at=NOW - timedelta(days=15), ends_at=NOW + timedelta(days=1)
    )
    session.add(period)
    session.flush()
    dto = official_invoice(
        issued_at=NOW - timedelta(hours=hours), channel=channel, supplier=supplier
    )
    row = make_invoice(
        period.id,
        external_invoice_id=dto.invoice_id,
        issued_at=dto.issued_at,
        external_updated_at=dto.updated_at,
        principal=dto.principal,
        interest=dto.interest,
        total=dto.total,
        channel=channel,
        counterparty_id=supplier,
    )
    session.add(row)
    session.commit()
    source = PaymentSource([dto, *extra])
    service = InvoicePaymentService(source, session, period, settings=make_settings())
    return service, row, dto


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channel,supplier", [("fabric", "1000"), ("buffer", "2000"), ("mercado", "4")]
)
async def test_pays_each_cost_type_without_local_order_and_persists_intent(
    db_session, channel, supplier
):
    service, row, dto = setup_payments(db_session, channel=channel, supplier=supplier)
    original = service.billing.pay_invoice

    async def pay(invoice_id):
        assert db_session.scalar(select(BillingTask.status)) == Status.IN_FLIGHT
        attempt = db_session.scalar(select(BillingAttempt))
        assert attempt.status == Status.IN_FLIGHT and attempt.finished_at is None
        return await original(invoice_id)

    service.billing.pay_invoice = pay
    result = await service.poll(now=NOW)
    assert result == {
        "confirmed": 1,
        "pending": 0,
        "uncertain": 0,
        "overdue": 0,
        "blocked": 0,
    }
    await service.poll(now=NOW + timedelta(minutes=1))
    assert service.billing.paid_ids == [dto.invoice_id]
    assert row.status == InvoiceStatus.PAID
    assert row.external_order_id is None
    assert db_session.scalar(select(BillingAttempt.status)) == Status.CONFIRMED
    assert (
        db_session.scalar(select(BillingAttempt.response))["invoice_id"]
        == dto.invoice_id
    )


@pytest.mark.anyio
async def test_discovery_enqueues_payment_in_same_transaction(db_session):
    service, _, dto = setup_payments(db_session)
    source = PaymentSource(
        [
            dto,
            official_invoice(
                invoice_id="buffer-new", channel="buffer", supplier="2000"
            ),
        ]
    )
    await BillingReconciliationService(
        source, db_session, service.period, settings=service.settings
    ).poll(now=NOW)
    assert db_session.scalar(select(func.count()).select_from(BillingTask)) == 2
    await InvoicePaymentService(
        source, db_session, service.period, settings=service.settings
    ).poll(now=NOW)
    assert set(source.paid_ids) == {dto.invoice_id, "buffer-new"}


@pytest.mark.anyio
async def test_pays_oldest_debts_first_even_after_grace_period(db_session, caplog):
    caplog.set_level("INFO")
    service, _, _ = setup_payments(db_session, hours=2)
    for index, hours in enumerate([6, 8, 5]):
        dto = official_invoice(
            invoice_id=f"cost-{index}", issued_at=NOW - timedelta(hours=hours)
        )
        service.billing.invoices.append(dto)
        db_session.add(
            make_invoice(
                service.period.id,
                external_invoice_id=dto.invoice_id,
                issued_at=dto.issued_at,
                external_updated_at=dto.updated_at,
                principal=dto.principal,
                interest=dto.interest,
                total=dto.total,
            )
        )
    db_session.commit()
    assert (await service.poll(now=NOW))["pending"] == 0
    assert service.billing.paid_ids == ["cost-1", "cost-0", "cost-2", "invoice-1"]
    assert "late=True" in caplog.text


@pytest.mark.anyio
async def test_paid_elsewhere_is_confirmed_without_another_payment(db_session):
    service, row, dto = setup_payments(db_session)
    service.billing.invoices = [dto.model_copy(update={"status": InvoiceStatus.PAID})]
    assert (await service.poll(now=NOW))["confirmed"] == 1
    assert row.status == InvoiceStatus.PAID
    assert service.billing.paid_ids == []


@pytest.mark.anyio
async def test_timeout_after_success_is_reconciled_by_id_after_restart(db_session):
    service, row, dto = setup_payments(db_session)
    original = service.billing.pay_invoice

    async def lose_response(invoice_id):
        await original(invoice_id)
        raise BillingTimeoutError("private-secret", outcome_uncertain=True)

    service.billing.pay_invoice = lose_response
    assert (await service.poll(now=NOW))["uncertain"] == 1
    restarted = InvoicePaymentService(
        service.billing, db_session, service.period, settings=service.settings
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=2)))["confirmed"] == 1
    assert service.billing.paid_ids == [dto.invoice_id]
    assert row.status == InvoiceStatus.PAID
    assert db_session.scalar(select(BillingAttempt.status)) == Status.CONFIRMED


class ProcessStopped(BaseException):
    pass


@pytest.mark.anyio
@pytest.mark.parametrize("remote_paid", [False, True])
async def test_crash_recovers_with_official_paid_and_pending_checks(
    db_session, remote_paid
):
    service, _, dto = setup_payments(db_session)
    original = service.billing.pay_invoice

    async def stop(invoice_id):
        if remote_paid:
            await original(invoice_id)
        raise ProcessStopped()

    service.billing.pay_invoice = stop
    with pytest.raises(ProcessStopped):
        await service.poll(now=NOW)
    assert db_session.scalar(select(BillingTask.status)) == Status.IN_FLIGHT
    service.billing.pay_invoice = original
    prior_queries = len(service.billing.calls)
    restarted = InvoicePaymentService(
        service.billing, db_session, service.period, settings=service.settings
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=2)))["confirmed"] == 1
    assert service.billing.paid_ids == [dto.invoice_id]
    recovery = service.billing.calls[prior_queries:]
    assert recovery[0]["status"] == InvoiceStatus.PAID
    if not remote_paid:
        assert recovery[1]["status"] == InvoiceStatus.PENDING


@pytest.mark.anyio
async def test_missing_or_failed_official_observation_never_triggers_payment(
    db_session,
):
    service, _, _ = setup_payments(db_session)
    service.billing.invoices = []
    result = await service.poll(now=NOW)
    assert result["pending"] == 1
    assert service.billing.paid_ids == []
    assert db_session.scalar(select(func.count()).select_from(BillingAttempt)) == 0
    service.billing.error = BillingSOAPFault("Server")
    await service.poll(now=NOW + timedelta(minutes=6))
    assert service.billing.paid_ids == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [{"client_id": "4"}, {"supplier_id": "2000"}, {"principal": Decimal(999)}],
)
async def test_rejects_invoice_identity_or_principal_changes(db_session, overrides):
    service, _, dto = setup_payments(db_session)
    service.billing.invoices = [dto.model_copy(update=overrides)]
    await service.poll(now=NOW)
    assert service.billing.paid_ids == []


@pytest.mark.anyio
async def test_supplier_side_invoices_and_other_periods_are_never_paid(db_session):
    service, row, _ = setup_payments(db_session)
    row.side = InvoiceSide.SUPPLIER
    other = make_period(code="other", starts_at=NOW - timedelta(days=1))
    db_session.add(other)
    db_session.flush()
    db_session.add(make_invoice(other.id))
    db_session.commit()
    assert (await service.poll(now=NOW))["pending"] == 0
    assert service.billing.paid_ids == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "receipt_failure",
    [ProcessStopped(), IntegrityError("private-sql", None, Exception())],
)
async def test_stored_paid_receipt_recovers_without_repeating_rpc(
    db_session, monkeypatch, receipt_failure
):
    service, row, dto = setup_payments(db_session)
    monkeypatch.setattr(service, "_confirm", Mock(side_effect=receipt_failure))
    if isinstance(receipt_failure, ProcessStopped):
        with pytest.raises(ProcessStopped):
            await service.poll(now=NOW)
    else:
        assert (await service.poll(now=NOW))["uncertain"] == 1
    assert (
        db_session.scalar(select(BillingAttempt.response))["invoice_id"]
        == dto.invoice_id
    )
    service.billing.error = BillingSOAPFault("Server")
    restarted = InvoicePaymentService(
        service.billing, db_session, service.period, settings=service.settings
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=2)))["confirmed"] == 1
    assert service.billing.paid_ids == [dto.invoice_id]
    assert row.status == InvoiceStatus.PAID


@pytest.mark.anyio
async def test_429_pauses_other_payments_across_restart(db_session):
    service, _, _ = setup_payments(db_session)
    service.billing.pay_error = BillingHTTPError(
        "private", status_code=429, retry_after_seconds=900
    )
    assert (await service.poll(now=NOW))["blocked"] == 1
    calls = len(service.billing.calls)
    restarted = InvoicePaymentService(
        service.billing, db_session, service.period, settings=service.settings
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=2)))["blocked"] == 1
    assert len(service.billing.calls) == calls
    assert len(service.billing.paid_ids) == 1


@pytest.mark.anyio
async def test_connection_failure_is_retried_only_after_rechecking_remote_status(
    db_session,
):
    service, _, dto = setup_payments(db_session)
    service.billing.pay_error = BillingConnectionError("private")
    assert (await service.poll(now=NOW))["pending"] == 1
    service.billing.pay_error = None
    assert (await service.poll(now=NOW + timedelta(minutes=2)))["confirmed"] == 1
    assert service.billing.paid_ids == [dto.invoice_id, dto.invoice_id]
    assert [q["status"] for q in service.billing.calls] == [
        InvoiceStatus.PAID,
        InvoiceStatus.PENDING,
    ] * 2


@pytest.mark.anyio
async def test_uses_official_interest_on_payment_not_local_estimate(db_session):
    service, row, dto = setup_payments(db_session, hours=8)
    original = service.billing.pay_invoice

    async def pay(invoice_id):
        paid = await original(invoice_id)
        return paid.model_copy(
            update={
                "interest": Decimal("2.123456789"),
                "total": Decimal("102.373456789"),
            }
        )

    service.billing.pay_invoice = pay
    await service.poll(now=NOW)
    assert row.interest == Decimal("2.123456789")
    assert row.total == Decimal("102.373456789")


@pytest.mark.parametrize(
    "seconds,percent",
    [
        (21599, "0"),
        (21600, "0"),
        (21601, "0"),
        (25199, "0"),
        (25200, "1"),
        (43200, "6"),
        (216000, "54"),
    ],
)
def test_simple_interest_uses_completed_hours_after_six_hour_grace(seconds, percent):
    assert estimated_interest(
        Decimal(100), NOW - timedelta(seconds=seconds), NOW
    ) == Decimal(percent)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "hours,severity", [(4, "warning"), (5, "urgent"), (6, "overdue")]
)
async def test_debt_alerts_escalate_and_include_failure_cause(
    db_session, caplog, hours, severity
):
    service, _, _ = setup_payments(db_session, hours=hours)
    service.billing.error = BillingSOAPFault("Server")
    result = await service.poll(now=NOW)
    assert result["pending"] == 1
    assert f"severity={severity}" in caplog.text
    assert "reason=BillingSOAPFault" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {"invoice_id": "another-invoice"},
        {"status": InvoiceStatus.PENDING},
        {"supplier_id": "4"},
    ],
)
async def test_invalid_payment_response_stays_uncertain_until_official_reconciliation(
    db_session, overrides
):
    service, row, _ = setup_payments(db_session)
    original = service.billing.pay_invoice

    async def invalid(invoice_id):
        return (await original(invoice_id)).model_copy(update=overrides)

    service.billing.pay_invoice = invalid
    result = await service.poll(now=NOW)
    assert result["uncertain"] == 1
    assert row.status == InvoiceStatus.PENDING
    assert db_session.scalar(select(BillingAttempt.response)) is not None
    restarted = InvoicePaymentService(
        service.billing, db_session, service.period, settings=service.settings
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=6)))["confirmed"] == 1
    assert service.billing.paid_ids == [row.external_invoice_id]


@pytest.mark.anyio
async def test_bad_old_debt_does_not_starve_valid_new_debts(db_session):
    service, row, _ = setup_payments(db_session, hours=8)
    service.settings.billing_payments_batch_size = 1
    young = official_invoice(invoice_id="young")
    service.billing.invoices = [young]
    db_session.add(
        make_invoice(
            service.period.id,
            external_invoice_id=young.invoice_id,
            issued_at=young.issued_at,
            external_updated_at=young.updated_at,
            principal=young.principal,
            interest=young.interest,
            total=young.total,
        )
    )
    db_session.commit()
    assert (await service.poll(now=NOW))["pending"] == 2
    assert service.billing.paid_ids == []
    assert (await service.poll(now=NOW + timedelta(minutes=1)))["pending"] == 1
    assert service.billing.paid_ids == ["young"]
    assert row.status == InvoiceStatus.PENDING


@pytest.mark.anyio
async def test_unrelated_paid_invoice_with_same_amount_does_not_hide_target_debt(
    db_session,
):
    service, row, _ = setup_payments(db_session)
    service.billing.invoices.append(
        official_invoice(invoice_id="unrelated", status="paid")
    )
    assert (await service.poll(now=NOW))["confirmed"] == 1
    assert service.billing.paid_ids == [row.external_invoice_id]


@pytest.mark.anyio
async def test_conflict_after_concurrent_payment_is_observed_without_second_payment(
    db_session,
):
    service, row, _ = setup_payments(db_session)
    original = service.billing.pay_invoice

    async def conflict(invoice_id):
        await original(invoice_id)
        raise BillingHTTPError("Already paid", status_code=409)

    service.billing.pay_invoice = conflict
    assert (await service.poll(now=NOW))["uncertain"] == 1
    assert (await service.poll(now=NOW + timedelta(minutes=2)))["confirmed"] == 1
    assert service.billing.paid_ids == [row.external_invoice_id]


@pytest.mark.anyio
async def test_future_period_does_not_pay_yet(db_session):
    service, _, _ = setup_payments(db_session)
    service.period.starts_at = NOW + timedelta(hours=1)
    db_session.commit()
    await service.poll(now=NOW)
    assert service.billing.paid_ids == []
    assert service.billing.calls == []


def test_wrong_group_is_rejected_before_payment(db_session):
    service, _, _ = setup_payments(db_session)
    with pytest.raises(ValueError, match="another group"):
        InvoicePaymentService(
            service.billing,
            db_session,
            service.period,
            settings=make_settings(farma_central_group=4),
        )


@pytest.mark.anyio
async def test_debt_older_than_three_days_is_paid_even_after_period_closed(db_session):
    service, row, _ = setup_payments(db_session, hours=96)
    service.period.ends_at = NOW - timedelta(days=3)
    db_session.commit()
    assert (await service.poll(now=NOW))["confirmed"] == 1
    assert row.status == InvoiceStatus.PAID
    for query in service.billing.calls:
        assert query["to_date"] - query["from_date"] <= timedelta(days=3)
        assert query["to_date"] < NOW - timedelta(days=2)
