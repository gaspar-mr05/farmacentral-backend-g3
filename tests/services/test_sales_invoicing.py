from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.clients.billing import (
    BillingConnectionError,
    BillingHTTPError,
    BillingSOAPFault,
    BillingTimeoutError,
)
from app.clients.purchase_orders import PurchaseOrdersHTTPError
from app.models import (
    BillingAttempt,
    BillingTask,
    ExternalOrderStatus,
    Invoice,
    InvoiceSide,
    MarketRole,
    OrderSource,
    OrderStatus,
)
from app.models import (
    BillingOperationStatus as Status,
)
from app.schemas.billing import BillingInvoice
from app.services.billing import BillingReconciliationService, queue
from app.services.billing.sales import SalesInvoicingService
from app.services.orders.state import apply_official_state
from tests.services.test_billing_reconciliation import BillingSource
from tests.support.billing import make_invoice, make_period, make_trade
from tests.support.billing_soap import invoice_values
from tests.support.farma_central import make_settings
from tests.support.orders import (
    EXTERNAL_ORDER_ID,
    make_external_order,
    make_purchase_order,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def sale_invoice(**overrides):
    return BillingInvoice.model_validate(
        invoice_values(
            **(
                {
                    "client": "farmacia-externa",
                    "supplier": "3",
                    "price": "3000",
                    "interest": "0",
                    "totalPrice": "3000",
                    "status": "pending",
                    "createdAt": NOW.isoformat(),
                    "updatedAt": NOW.isoformat(),
                }
                | overrides
            )
        )
    )


def setup_sales(session, *, source=OrderSource.SFTP, market=False):
    period = make_period(
        starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=1)
    )
    session.add(period)
    session.flush()
    order = None
    if market:
        session.add(
            make_trade(
                period.id,
                external_order_id=EXTERNAL_ORDER_ID,
                role=MarketRole.SELLER,
                counterparty_id="farmacia-externa",
                closed_at=NOW,
            )
        )
    else:
        order = make_external_order(
            source=source,
            status=OrderStatus.DISPATCHED,
            external_status=ExternalOrderStatus.FULFILLED,
            created_at=NOW - timedelta(hours=1),
        )
        if source == OrderSource.WEB:
            order.buyer_email = "buyer@example.test"
        session.add(order)
    session.commit()
    settings = make_settings(app_env="dev", billing_period_id=str(period.id))
    official = make_purchase_order(
        estado="cumplida",
        despachado=2,
        creada=(NOW - timedelta(hours=1)).isoformat(),
        actualizada=NOW.isoformat(),
    )
    billing = Mock(
        emit_invoice=AsyncMock(return_value=sale_invoice()), pay_invoice=AsyncMock()
    )
    orders = Mock(get_order=AsyncMock(return_value=official))
    service = SalesInvoicingService(billing, orders, session, period, settings=settings)
    return service, order, official


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source",
    [OrderSource.SFTP, OrderSource.PUBSUB, OrderSource.MESSAGING, OrderSource.MARKET],
)
async def test_invoices_each_sales_channel_once_with_durable_intent(db_session, source):
    service, order, _ = setup_sales(
        db_session, source=source, market=source == OrderSource.MARKET
    )

    async def emit(order_id):
        assert order_id == EXTERNAL_ORDER_ID
        task = db_session.scalar(select(BillingTask))
        attempt = db_session.scalar(select(BillingAttempt))
        assert task.status == attempt.status == Status.IN_FLIGHT
        assert attempt.finished_at is None
        assert attempt.response is None
        service.orders.get_order.assert_awaited_once_with(order_id)
        return sale_invoice()

    service.billing.emit_invoice.side_effect = emit
    result = await service.poll(now=NOW)
    assert result == {"confirmed": 1, "missing": 0, "uncertain": 0, "blocked": 0}
    await service.poll(now=NOW + timedelta(minutes=1))
    service.billing.emit_invoice.assert_awaited_once_with(EXTERNAL_ORDER_ID)
    service.billing.pay_invoice.assert_not_awaited()
    invoice = db_session.scalar(select(Invoice))
    assert invoice.external_order_id == EXTERNAL_ORDER_ID
    assert invoice.side == InvoiceSide.SUPPLIER
    assert invoice.principal == Decimal("3000")
    assert invoice.sale_order_id == (order.id if order else None)
    assert bool(invoice.market_trade_id) == (source == OrderSource.MARKET)
    assert db_session.scalar(select(BillingAttempt.status)) == Status.CONFIRMED


@pytest.mark.anyio
async def test_legacy_web_checkout_without_official_id_is_never_invoiced(db_session):
    service, order, _ = setup_sales(db_session, source=OrderSource.WEB)
    order.external_order_id = None
    db_session.commit()
    assert (await service.poll(now=NOW))["confirmed"] == 0
    service.orders.get_order.assert_not_awaited()
    service.billing.emit_invoice.assert_not_awaited()


@pytest.mark.anyio
async def test_web_sale_with_real_official_id_can_be_invoiced(db_session):
    service, _, _ = setup_sales(db_session, source=OrderSource.WEB)
    assert (await service.poll(now=NOW))["confirmed"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides",
    [
        {"supplier_id": "4"},
        {"state": "aceptada"},
        {"created_at": NOW - timedelta(days=10)},
    ],
)
async def test_checks_official_supplier_fulfillment_and_period_before_emitting(
    db_session, overrides
):
    service, _, official = setup_sales(db_session)
    service.orders.get_order.return_value = official.model_copy(update=overrides)
    assert (await service.poll(now=NOW))["missing"] == 1
    service.billing.emit_invoice.assert_not_awaited()
    assert db_session.scalar(select(func.count()).select_from(BillingAttempt)) == 0


@pytest.mark.anyio
async def test_already_invoiced_sale_without_exact_association_stays_uncertain(
    db_session,
):
    service, _, official = setup_sales(db_session)
    service.orders.get_order.return_value = official.model_copy(
        update={"invoiced": True}
    )
    # Same amount and date are insufficient evidence of an order-invoice link.
    db_session.add(
        make_invoice(
            service.period.id,
            side=InvoiceSide.SUPPLIER,
            counterparty_id="farmacia-externa",
            principal=Decimal(3000),
            total=Decimal(3000),
            issued_at=NOW,
            external_updated_at=NOW,
        )
    )
    db_session.commit()
    assert (await service.poll(now=NOW))["uncertain"] == 1
    service.billing.emit_invoice.assert_not_awaited()
    assert db_session.scalar(select(Invoice.external_order_id)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("invoiced", [False, True])
async def test_lost_response_is_not_resent_after_restart(db_session, invoiced):
    service, _, official = setup_sales(db_session)
    service.billing.emit_invoice.side_effect = BillingTimeoutError(
        "secret", outcome_uncertain=True
    )
    assert (await service.poll(now=NOW))["uncertain"] == 1
    service.orders.get_order.return_value = official.model_copy(
        update={"invoiced": invoiced}
    )
    restarted = SalesInvoicingService(
        service.billing,
        service.orders,
        db_session,
        service.period,
        settings=service.settings,
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=11)))["uncertain"] == 1
    service.billing.emit_invoice.assert_awaited_once()
    attempt = db_session.scalar(select(BillingAttempt))
    assert attempt.status == Status.UNCERTAIN
    assert attempt.error_type == "BillingTimeoutError"
    assert "secret" not in db_session.scalar(select(BillingTask.last_error))


class ProcessStopped(BaseException):
    pass


@pytest.mark.anyio
async def test_crash_during_soap_recovers_intent_without_duplicate_emission(db_session):
    service, _, _ = setup_sales(db_session)
    service.billing.emit_invoice.side_effect = ProcessStopped()
    with pytest.raises(ProcessStopped):
        await service.poll(now=NOW)
    assert db_session.scalar(select(BillingTask.status)) == Status.IN_FLIGHT
    restarted = SalesInvoicingService(
        service.billing,
        service.orders,
        db_session,
        service.period,
        settings=service.settings,
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=1)))["uncertain"] == 1
    assert db_session.scalar(select(BillingAttempt.status)) == Status.UNCERTAIN
    service.billing.emit_invoice.assert_awaited_once()


@pytest.mark.anyio
async def test_persisted_emission_receipt_recovers_exact_link_after_crash(
    db_session, monkeypatch
):
    service, _, official = setup_sales(db_session)
    monkeypatch.setattr(service, "_confirm", Mock(side_effect=ProcessStopped()))
    with pytest.raises(ProcessStopped):
        await service.poll(now=NOW)
    assert (
        db_session.scalar(select(BillingAttempt.response))["order_id"]
        == EXTERNAL_ORDER_ID
    )
    service.orders.get_order.return_value = official.model_copy(
        update={"invoiced": True}
    )
    restarted = SalesInvoicingService(
        service.billing,
        service.orders,
        db_session,
        service.period,
        settings=service.settings,
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=1)))["confirmed"] == 1
    service.billing.emit_invoice.assert_awaited_once()
    assert db_session.scalar(select(Invoice.external_order_id)) == EXTERNAL_ORDER_ID
    assert db_session.scalar(select(BillingAttempt.status)) == Status.CONFIRMED


@pytest.mark.anyio
async def test_ledger_integrity_failure_keeps_receipt_and_does_not_repeat_emission(
    db_session, monkeypatch
):
    service, _, official = setup_sales(db_session)
    monkeypatch.setattr(
        service,
        "_confirm",
        Mock(side_effect=IntegrityError("private-sql", None, Exception())),
    )
    assert (await service.poll(now=NOW))["uncertain"] == 1
    attempt = db_session.scalar(select(BillingAttempt))
    assert attempt.response["order_id"] == EXTERNAL_ORDER_ID
    assert attempt.error_type == "IntegrityError"
    assert "private-sql" not in db_session.scalar(select(BillingTask.last_error))
    service.orders.get_order.return_value = official.model_copy(
        update={"invoiced": True}
    )
    restarted = SalesInvoicingService(
        service.billing,
        service.orders,
        db_session,
        service.period,
        settings=service.settings,
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=11)))["confirmed"] == 1
    service.billing.emit_invoice.assert_awaited_once()


@pytest.mark.anyio
async def test_connection_failure_before_send_retries_with_another_audited_attempt(
    db_session,
):
    service, _, _ = setup_sales(db_session)
    service.billing.emit_invoice.side_effect = [
        BillingConnectionError("private"),
        sale_invoice(),
    ]
    assert (await service.poll(now=NOW))["missing"] == 1
    await service.poll(now=NOW + timedelta(seconds=30))
    assert service.billing.emit_invoice.await_count == 1
    assert (await service.poll(now=NOW + timedelta(minutes=2)))["confirmed"] == 1
    assert db_session.scalars(
        select(BillingAttempt.status).order_by(BillingAttempt.number)
    ).all() == [Status.FAILED, Status.CONFIRMED]


@pytest.mark.anyio
async def test_permanent_soap_rejection_is_not_retried_automatically(db_session):
    service, _, _ = setup_sales(db_session)
    service.billing.emit_invoice.side_effect = BillingSOAPFault("Client")
    assert (await service.poll(now=NOW))["missing"] == 1
    await service.poll(now=NOW + timedelta(hours=1))
    service.billing.emit_invoice.assert_awaited_once()
    assert db_session.scalar(select(BillingAttempt.status)) == Status.FAILED


@pytest.mark.anyio
async def test_mismatched_invoice_response_is_saved_for_audit_but_not_linked(
    db_session,
):
    service, _, _ = setup_sales(db_session)
    service.billing.emit_invoice.return_value = sale_invoice(price="100")
    assert (await service.poll(now=NOW))["uncertain"] == 1
    assert db_session.scalar(select(BillingAttempt.response)) is not None
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("preflight", [False, True])
async def test_rate_limit_blocks_other_sales_and_survives_restart(
    db_session, preflight
):
    service, _, _ = setup_sales(db_session)
    db_session.add(
        make_external_order(
            external_order_id="1" * 24,
            status=OrderStatus.DISPATCHED,
            external_status=ExternalOrderStatus.FULFILLED,
            created_at=NOW - timedelta(hours=1),
        )
    )
    db_session.commit()
    if preflight:
        service.orders.get_order.side_effect = PurchaseOrdersHTTPError(
            429, retry_after_seconds=900
        )
    else:
        service.billing.emit_invoice.side_effect = BillingHTTPError(
            "private", status_code=429, retry_after_seconds=900
        )
    assert (await service.poll(now=NOW))["blocked"] == 1
    first_calls = service.orders.get_order.await_count
    restarted = SalesInvoicingService(
        service.billing,
        service.orders,
        db_session,
        service.period,
        settings=service.settings,
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=1)))["blocked"] == 1
    assert service.orders.get_order.await_count == first_calls


def test_confirmation_enqueues_in_same_transaction_and_is_idempotent(
    db_session, monkeypatch
):
    service, order, _ = setup_sales(db_session)
    monkeypatch.setattr(queue, "get_settings", lambda: service.settings)
    apply_official_state(db_session, order, ExternalOrderStatus.FULFILLED)
    db_session.commit()
    apply_official_state(db_session, order, ExternalOrderStatus.FULFILLED)
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(BillingTask)) == 1
    assert db_session.scalar(select(BillingTask.status)) == Status.READY
    assert db_session.scalar(select(func.count()).select_from(BillingAttempt)) == 0


def test_rolled_back_fulfillment_does_not_leave_a_billing_task(db_session, monkeypatch):
    service, order, _ = setup_sales(db_session)
    monkeypatch.setattr(queue, "get_settings", lambda: service.settings)
    order.external_status = ExternalOrderStatus.ACCEPTED
    db_session.commit()
    apply_official_state(db_session, order, ExternalOrderStatus.FULFILLED)
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(BillingTask)) == 0
    assert order.external_status == ExternalOrderStatus.ACCEPTED


@pytest.mark.anyio
async def test_emission_preserves_decimal_prices(db_session):
    service, _, official = setup_sales(db_session)
    service.orders.get_order.return_value = official.model_copy(
        update={"unit_price": Decimal("1500.123456789")}
    )
    service.billing.emit_invoice.return_value = sale_invoice(
        price="3000.246913578", totalPrice="3000.246913578"
    )
    assert (await service.poll(now=NOW))["confirmed"] == 1
    assert db_session.scalar(select(Invoice.principal)) == Decimal("3000.246913578")


@pytest.mark.anyio
async def test_reconciliation_observes_customer_payment_without_paying_sale(db_session):
    service, _, _ = setup_sales(db_session)
    await service.poll(now=NOW)
    paid_at = NOW + timedelta(minutes=2)
    source = BillingSource([sale_invoice(status="paid", updatedAt=paid_at.isoformat())])
    await BillingReconciliationService(
        source, db_session, service.period, settings=service.settings
    ).poll(now=paid_at)
    invoice = db_session.scalar(select(Invoice))
    assert invoice.external_order_id == EXTERNAL_ORDER_ID
    assert invoice.status.value == "paid"
    assert invoice.paid_at == paid_at
    assert db_session.scalar(select(BillingTask.invoice_id)) == invoice.id
    service.billing.pay_invoice.assert_not_awaited()


@pytest.mark.anyio
async def test_known_exact_invoice_link_confirms_task_without_reemitting(db_session):
    service, _, _ = setup_sales(db_session)
    db_session.add(
        make_invoice(
            service.period.id,
            side=InvoiceSide.SUPPLIER,
            external_order_id=EXTERNAL_ORDER_ID,
        )
    )
    db_session.commit()
    assert (await service.poll(now=NOW))["confirmed"] == 1
    service.billing.emit_invoice.assert_not_awaited()
    service.orders.get_order.assert_not_awaited()


@pytest.mark.anyio
async def test_old_sales_and_market_purchases_are_excluded(db_session):
    service, order, _ = setup_sales(db_session)
    order.created_at = NOW - timedelta(days=10)
    db_session.add(
        make_trade(
            service.period.id,
            role=MarketRole.BUYER,
            external_order_id="1" * 24,
            closed_at=NOW,
        )
    )
    db_session.commit()
    assert (await service.poll(now=NOW))["missing"] == 0
    service.orders.get_order.assert_not_awaited()
    service.billing.emit_invoice.assert_not_awaited()


@pytest.mark.anyio
async def test_batch_sweep_reports_unqueued_sales_and_catches_them_next_cycle(
    db_session,
):
    service, _, _ = setup_sales(db_session)
    service.settings.billing_sales_batch_size = 1
    db_session.add(
        make_external_order(
            external_order_id="1" * 24,
            status=OrderStatus.DISPATCHED,
            external_status=ExternalOrderStatus.FULFILLED,
            created_at=NOW - timedelta(hours=1),
        )
    )
    db_session.commit()

    async def get_order(order_id):
        return make_purchase_order(
            id=order_id,
            estado="cumplida",
            despachado=2,
            creada=(NOW - timedelta(hours=1)).isoformat(),
        )

    async def emit(order_id):
        return sale_invoice(id=order_id)

    service.orders.get_order.side_effect = get_order
    service.billing.emit_invoice.side_effect = emit
    first = await service.poll(now=NOW)
    assert (first["confirmed"], first["missing"]) == (1, 1)
    second = await service.poll(now=NOW + timedelta(minutes=1))
    assert (second["confirmed"], second["missing"]) == (2, 0)
