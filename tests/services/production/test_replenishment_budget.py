from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models import (
    BillingSyncState,
    InvoiceSide,
    MarketOffer,
    ReplenishmentState,
)
from app.services.billing.metrics import BusinessMetricsService
from app.services.production.replenishment_budget import spending_room
from tests.services.test_procurement_planning import product
from tests.support.billing import make_invoice, make_period
from tests.support.farma_central import make_settings


def setup(session):
    period = make_period()
    session.add(period)
    session.flush()
    state = ReplenishmentState(period_id=period.id, sku="KIT-A", estimated_spend=30)
    state.pending_operation = {"kind": "supply", "cost": "10", "status": "confirmed"}
    session.add(state)
    session.add(
        MarketOffer(
            period_id=period.id,
            side="buy",
            publisher_group_id=3,
            sku="RAW",
            quantity=3,
            unit_price=5,
            status="open",
        )
    )
    session.commit()
    return period


def room(session, period, *, limit=100):
    return spending_room(
        session,
        period,
        make_settings(replenishment_spending_limit=limit),
        {"KIT-A": product("KIT-A", components=[("RAW", 1)])},
        {"KIT-A": Decimal(10)},
    )


def test_full_past_and_pending_costs_reduce_period_budget(db_session):
    period = setup(db_session)
    amount, detail = room(db_session, period)
    assert amount == 45
    assert detail["period_spent"] == "30"
    assert detail["pending_outlay"] == "25"


def test_whole_invoices_and_interest_still_count_in_business_margin(db_session):
    period = setup(db_session)
    for name, side, principal, interest in (
        ("sale", InvoiceSide.SUPPLIER, 50, 0),
        ("purchase", InvoiceSide.CLIENT, 20, 2),
    ):
        db_session.add(
            make_invoice(
                period.id,
                external_invoice_id=name,
                side=side,
                principal=principal,
                interest=interest,
                total=principal + interest,
            )
        )
    db_session.commit()
    amount, detail = room(db_session, period)
    assert amount == 73
    assert Decimal(detail["invoiced_margin"]) == 28
    metrics = BusinessMetricsService(db_session, period).calculate()
    assert metrics.invoiced_costs == 20 and metrics.client_interest == 2
    assert metrics.margin == 28


def test_invoiced_loss_reduces_envelope_and_does_not_invent_future_sales(db_session):
    period = setup(db_session)
    db_session.add(
        make_invoice(
            period.id,
            principal=60,
            interest=0,
            total=60,
        )
    )
    db_session.commit()
    amount, detail = room(db_session, period)
    assert amount == 15 and Decimal(detail["invoiced_margin"]) == -60


def test_same_purchase_estimate_and_invoice_are_not_deducted_twice(db_session):
    period = setup(db_session)
    db_session.add(make_invoice(period.id, principal=30, interest=0, total=30))
    db_session.commit()
    assert room(db_session, period)[0] == 45


@pytest.mark.parametrize("balance", [0, -100, 40])
def test_accounting_balance_does_not_replace_spending_budget(db_session, balance):
    period = setup(db_session)
    db_session.add(
        BillingSyncState(
            period_id=period.id,
            stream="bank",
            balance=balance,
            last_success_at=datetime.now(UTC),
        )
    )
    db_session.add(make_invoice(period.id, principal=5, interest=0, total=5))
    db_session.commit()
    amount, detail = room(db_session, period)
    assert amount == 45
    assert Decimal(detail["accounting_balance"]) == balance


def test_default_budget_is_one_configured_target_basket(db_session):
    period = make_period()
    db_session.add(period)
    db_session.commit()
    amount, detail = room(db_session, period, limit=None)
    assert amount == 60 and Decimal(detail["period_limit"]) == 60
