from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.models import (
    BillingSyncState,
    BusinessMetricSnapshot,
    ExternalOrderStatus,
    InvoiceSide,
    InvoiceStatus,
    MarketRole,
    OrderStatus,
)
from app.services.billing import BusinessMetricsService
from tests.support.billing import make_invoice, make_period, make_trade
from tests.support.orders import EXTERNAL_ORDER_ID, make_external_order

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def setup_metrics(session):
    period = make_period(
        starts_at=NOW - timedelta(days=1), ends_at=NOW + timedelta(days=1)
    )
    session.add(period)
    session.flush()
    session.add(
        make_external_order(
            status=OrderStatus.DISPATCHED,
            external_status=ExternalOrderStatus.FULFILLED,
            created_at=NOW - timedelta(hours=6),
        )
    )
    session.add_all(
        [
            make_invoice(
                period.id,
                external_invoice_id="sale",
                external_order_id=EXTERNAL_ORDER_ID,
                side=InvoiceSide.SUPPLIER,
                status=InvoiceStatus.PAID,
                channel="sftp",
                principal=Decimal("200"),
                interest=Decimal("5"),
                total=Decimal("205"),
            ),
            make_invoice(
                period.id,
                external_invoice_id="fabric",
                channel="fabric",
                principal=Decimal("100"),
                interest=Decimal("2"),
                total=Decimal("102"),
                issued_at=NOW - timedelta(hours=5),
            ),
            make_invoice(
                period.id,
                external_invoice_id="buffer",
                channel="buffer",
                status=InvoiceStatus.PAID,
                principal=Decimal("20"),
                interest=Decimal("1"),
                total=Decimal("21"),
            ),
            make_invoice(
                period.id,
                external_invoice_id="market",
                channel="mercado",
                principal=Decimal("30"),
                interest=Decimal("3"),
                total=Decimal("33"),
            ),
            make_trade(period.id, role=MarketRole.BUYER),
            make_trade(
                period.id,
                external_offer_id="offer-2",
                external_order_id=EXTERNAL_ORDER_ID,
                role=MarketRole.SELLER,
            ),
        ]
    )
    for index, stream in enumerate(
        ("client:pending", "client:paid", "supplier:pending", "supplier:paid")
    ):
        session.add(
            BillingSyncState(
                period_id=period.id,
                stream=stream,
                last_success_at=NOW - timedelta(minutes=index + 1),
            )
        )
    session.add(
        BillingSyncState(period_id=period.id, stream="bank", balance=Decimal("123.45"))
    )
    session.commit()
    return period


def test_calculates_accrued_margin_and_operational_metrics(db_session):
    period = setup_metrics(db_session)
    report = BusinessMetricsService(db_session, period).calculate(now=NOW)

    assert report.invoiced_income == Decimal("200")
    assert report.invoiced_costs == Decimal("150")
    assert report.client_interest == Decimal("6")
    assert report.margin == Decimal("44")
    assert report.costs_by_channel == {
        "fabric": Decimal("100"),
        "buffer": Decimal("20"),
        "market": Decimal("30"),
        "other": Decimal("0"),
    }
    assert report.bank_balance == Decimal("123.45")
    assert report.invoicing.model_dump() == {
        "fulfilled_sales": 1,
        "confirmed": 1,
        "uncertain": 0,
        "missing": 0,
        "ratio": Decimal("1"),
    }
    assert report.pending_payments.count == 2
    assert report.pending_payments.oldest_age_seconds == 5 * 60 * 60
    assert report.reconciliation.last_complete_at == NOW - timedelta(minutes=4)
    assert report.reconciliation.age_seconds == 4 * 60
    assert (report.market_purchases, report.market_sales) == (1, 1)


def test_persists_periodic_and_closing_snapshots_without_duplicates(db_session):
    period = setup_metrics(db_session)
    service = BusinessMetricsService(db_session, period)

    first = service.capture("abc123", now=NOW, minimum_interval=timedelta(minutes=10))
    skipped = service.capture(
        "abc123",
        now=NOW + timedelta(minutes=5),
        minimum_interval=timedelta(minutes=10),
    )
    closing = service.capture(
        "def456",
        now=NOW + timedelta(minutes=5),
        is_closing=True,
        minimum_interval=timedelta(minutes=10),
    )

    assert first is not None
    assert skipped is None
    assert closing.is_closing is True
    assert closing.metrics["margin"] == "44"
    assert (
        db_session.scalar(select(func.count()).select_from(BusinessMetricSnapshot)) == 2
    )
