from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.clients.billing import BillingInvalidResponseError
from app.models import BillingTask, InvoiceSide, InvoiceStatus, MarketRole
from app.schemas.billing import BillingInvoice
from app.services.billing.invoices import upsert_invoice
from tests.support.billing import make_period, make_trade
from tests.support.billing_soap import invoice_values


def scenario(session, **changes):
    now = datetime.now(UTC)
    period = make_period(starts_at=now - timedelta(hours=1))
    session.add(period)
    session.flush()
    trade = make_trade(
        period.id,
        external_order_id="a" * 24,
        quantity=3,
        unit_price=Decimal("7.50"),
        closed_at=now - timedelta(minutes=1),
    )
    session.add(trade)
    session.flush()
    invoice = BillingInvoice.model_validate(
        invoice_values(
            **(
                {
                    "supplier": "4",
                    "client": "3",
                    "channel": "mercado",
                    "price": "22.50",
                    "interest": "0.90",
                    "totalPrice": "23.40",
                    "status": "paid",
                    "createdAt": now.isoformat(),
                    "updatedAt": now.isoformat(),
                    "description": "Charge for 3 unit(s) at $7.50 each via mercado "
                    f"channel (order {trade.external_order_id})",
                }
                | changes
            )
        )
    )
    return period, trade, invoice, now


def test_paid_invoice_with_explicit_reference_is_linked_without_queuing_payment(
    db_session,
):
    period, trade, invoice, now = scenario(db_session)
    row = upsert_invoice(db_session, period, invoice, InvoiceSide.CLIENT, now)
    db_session.flush()
    assert row.external_order_id == trade.external_order_id
    assert row.market_trade_id == trade.id
    assert row.status == InvoiceStatus.PAID
    assert row.interest == Decimal("0.90")
    assert db_session.scalar(select(func.count()).select_from(BillingTask)) == 0
    again = upsert_invoice(db_session, period, invoice, InvoiceSide.CLIENT, now)
    assert again.id == row.id


@pytest.mark.parametrize(
    "description",
    [
        "Charge for 3 units; no official order reference",
        "Order " + "a" * 24 + " and order " + "b" * 24,
        "Charge (order " + "b" * 24 + ")",
    ],
)
def test_amount_alone_or_ambiguous_reference_never_links(db_session, description):
    period, _, invoice, now = scenario(db_session, description=description)
    row = upsert_invoice(db_session, period, invoice, InvoiceSide.CLIENT, now)
    assert row.external_order_id is None and row.market_trade_id is None


@pytest.mark.parametrize(
    "changes",
    [
        {"supplier": "5"},
        {"client": "9"},
        {"price": "23", "totalPrice": "23.90"},
    ],
)
def test_explicit_reference_must_match_counterparties_and_principal(
    db_session, changes
):
    period, _, invoice, now = scenario(db_session, **changes)
    with pytest.raises(BillingInvalidResponseError):
        upsert_invoice(db_session, period, invoice, InvoiceSide.CLIENT, now)


def test_supplier_invoice_matches_seller_role(db_session):
    period, trade, invoice, now = scenario(db_session, supplier="3", client="4")
    trade.role = MarketRole.SELLER
    db_session.flush()
    row = upsert_invoice(db_session, period, invoice, InvoiceSide.SUPPLIER, now)
    assert row.market_trade_id == trade.id


def test_reference_to_trade_in_another_period_is_not_linked(db_session):
    period, trade, invoice, now = scenario(db_session)
    other = make_period(code="other-period")
    db_session.add(other)
    db_session.flush()
    trade.period_id = other.id
    db_session.flush()
    row = upsert_invoice(db_session, period, invoice, InvoiceSide.CLIENT, now)
    assert row.external_order_id is None and row.market_trade_id is None
