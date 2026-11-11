from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    BillingSyncState,
    BusinessMetricSnapshot,
    ExternalOrderStatus,
    Invoice,
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
                cursor=NOW,
                full_started_at=NOW - timedelta(minutes=5),
                full_completed_at=NOW - timedelta(minutes=index + 1),
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
    prepare_closing(db_session, period)
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


def prepare_closing(session, period):
    period.ends_at = NOW
    hours = {"sftp": 1, "fabric": 8, "buffer": 12, "mercado": 16}
    for invoice in session.scalars(
        select(Invoice).where(Invoice.period_id == period.id)
    ):
        invoice.issued_at = NOW - timedelta(hours=hours[invoice.channel])
        invoice.observed_at = NOW + timedelta(minutes=2)
        if invoice.status == InvoiceStatus.PAID:
            invoice.paid_at = NOW - timedelta(minutes=1)
    for state in session.scalars(
        select(BillingSyncState).where(
            BillingSyncState.period_id == period.id, BillingSyncState.stream != "bank"
        )
    ):
        state.cursor = NOW
        state.full_started_at = NOW + timedelta(minutes=1)
        state.full_completed_at = NOW + timedelta(minutes=2)
        state.last_error = None
    session.commit()


def test_query_success_does_not_hide_incomplete_discovery(db_session):
    period = setup_metrics(db_session)
    for state in db_session.scalars(
        select(BillingSyncState).where(BillingSyncState.stream != "bank")
    ):
        state.cursor = NOW - timedelta(days=5)
        state.last_success_at = NOW
    db_session.commit()
    report = BusinessMetricsService(db_session, period).calculate(now=NOW)
    assert report.reconciliation.covered_through == NOW - timedelta(days=5)
    assert report.reconciliation.age_seconds == 5 * 86400


def test_success_without_a_full_scan_is_not_complete(db_session):
    period = setup_metrics(db_session)
    for state in db_session.scalars(select(BillingSyncState)):
        state.full_completed_at = None
    db_session.commit()
    report = BusinessMetricsService(db_session, period).calculate(now=NOW)
    assert report.reconciliation.last_complete_at is None
    assert report.reconciliation.full_scan_pending


def test_closing_rejects_stale_interest_and_incomplete_scans(db_session):
    period = setup_metrics(db_session)
    service = BusinessMetricsService(db_session, period)
    with pytest.raises(ValueError, match="finished accounting period"):
        service.capture("abc", now=NOW, is_closing=True)
    period.ends_at = NOW
    db_session.commit()
    with pytest.raises(ValueError, match="full reconciliation"):
        service.capture("abc", now=NOW + timedelta(minutes=3), is_closing=True)
    prepare_closing(db_session, period)
    invoice = db_session.scalar(select(Invoice).where(Invoice.channel == "fabric"))
    invoice.interest = 0
    db_session.commit()
    with pytest.raises(ValueError, match="period-end amount"):
        service.capture("abc", now=NOW + timedelta(minutes=3), is_closing=True)
    invoice.interest = 2
    invoice.observed_at = NOW - timedelta(hours=1)
    db_session.commit()
    with pytest.raises(ValueError, match="stale pending"):
        service.capture("abc", now=NOW + timedelta(minutes=3), is_closing=True)


def test_interest_breakdown_and_closing_cutoff(db_session):
    period = setup_metrics(db_session)
    prepare_closing(db_session, period)
    db_session.add(
        make_invoice(
            period.id,
            external_invoice_id="after-end",
            issued_at=NOW + timedelta(minutes=1),
            principal=999,
            interest=0,
        )
    )
    db_session.commit()
    report = (
        BusinessMetricsService(db_session, period)
        .capture("abc", now=NOW + timedelta(minutes=3), is_closing=True)
        .metrics
    )
    assert report["margin"] == "44"
    assert report["client_interest_paid"] == "1"
    assert report["client_interest_pending"] == "5"
