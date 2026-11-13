import asyncio
import multiprocessing
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import BillingOperationStatus, BillingTask, BusinessPeriod
from tests.services.test_invoice_payments import NOW, setup_payments
from tests.services.test_recalls import recall_database as recall_database
from tests.services.test_sales_invoicing import setup_sales


def process_invoice(url, kind, period_id, task_id, entered, release, calls):
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            if kind == "pay":
                from datetime import timedelta

                from app.services.billing.payments import InvoicePaymentService
                from tests.services.test_invoice_payments import (
                    PaymentSource,
                    official_invoice,
                )
                from tests.support.farma_central import make_settings

                remote = PaymentSource(
                    [official_invoice(issued_at=NOW - timedelta(hours=1))]
                )
                service = InvoicePaymentService(
                    remote,
                    session,
                    session.get(BusinessPeriod, period_id),
                    settings=make_settings(),
                )
                original = remote.pay_invoice

                async def effect(target):
                    with calls.get_lock():
                        calls.value += 1
                    entered.set()
                    assert release.wait(15)
                    return await original(target)

                remote.pay_invoice = effect
                service._clock = lambda: NOW
                asyncio.run(service._process(session.get(BillingTask, task_id)))
            else:
                from datetime import timedelta

                from app.services.billing.sales import SalesInvoicingService
                from tests.services.test_sales_invoicing import sale_invoice
                from tests.support.farma_central import make_settings
                from tests.support.orders import make_purchase_order

                remote = AsyncMock()

                async def effect(target):
                    with calls.get_lock():
                        calls.value += 1
                    entered.set()
                    assert release.wait(15)
                    return sale_invoice()

                remote.emit_invoice.side_effect = effect
                orders = AsyncMock()
                orders.get_order.return_value = make_purchase_order(
                    estado="cumplida",
                    despachado=2,
                    creada=(NOW - timedelta(hours=1)).isoformat(),
                )
                service = SalesInvoicingService(
                    remote,
                    orders,
                    session,
                    session.get(BusinessPeriod, period_id),
                    settings=make_settings(),
                )
                asyncio.run(service._process(session.get(BillingTask, task_id), NOW))
    finally:
        engine.dispose()


@pytest.mark.parametrize("kind", ["emit", "pay"])
def test_two_processes_cannot_send_the_same_financial_operation(recall_database, kind):
    with Session(recall_database) as session:
        service, *_ = setup_payments(session) if kind == "pay" else setup_sales(session)
        service._sweep()
        task = session.scalar(select(BillingTask))
        task_id, period_id = task.id, service.period.id
        schema = session.connection().exec_driver_sql("SHOW search_path").scalar_one()
        url = recall_database.url.update_query_dict(
            {"options": f"-csearch_path={schema}"}
        ).render_as_string(hide_password=False)
        session.commit()
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    calls = context.Value("i", 0)
    args = (url, kind, period_id, task_id, entered, release, calls)
    first, second = (
        context.Process(target=process_invoice, args=args),
        context.Process(target=process_invoice, args=args),
    )
    try:
        first.start()
        assert entered.wait(15)
        second.start()
        second.join(15)
        assert second.exitcode == 0
        assert calls.value == 1
        release.set()
        first.join(15)
        assert first.exitcode == 0
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(5)
    with Session(recall_database) as session:
        assert (
            session.get(BillingTask, task_id).status == BillingOperationStatus.CONFIRMED
        )


@pytest.mark.anyio
async def test_uncertain_market_acceptance_is_observed_not_reposted(db_session):
    from app.models import MarketOffer
    from app.services.trades.custody import InvalidTradeStateError
    from tests.services.test_trade_custody import (
        FarmaCentralStub,
        add_market_stock,
        make_service,
        official_offer,
    )

    period, _, dispatch, units = add_market_stock(db_session)
    official = official_offer(sku=units[0].lot.product.sku, publisher_group=4)
    remote = AsyncMock()
    remote.accept_offer.side_effect = TimeoutError("remote response lost")
    remote.get_offer.return_value = official
    service = make_service(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=remote,
        period=period,
    )
    with pytest.raises(TimeoutError):
        await service.accept_sell(official)
    with pytest.raises(InvalidTradeStateError, match="POST not repeated"):
        await service.accept_sell(official)
    remote.accept_offer.assert_awaited_once()
    remote.get_offer.assert_awaited_once()
    assert db_session.scalar(select(MarketOffer.status)) == "acceptance_uncertain"
