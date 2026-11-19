from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.models import (
    CustodyEvent,
    CustodyEventType,
    MarketOffer,
    MarketRole,
    MarketTrade,
    ReplenishmentState,
)
from app.services.production.replenishment import E3ReplenishmentService
from app.services.production.supply_receipts import (
    supply_receipt_baseline,
    supply_resume_basis,
)
from tests.services.test_trade_custody import add_market_stock


def receive(session, units):
    for unit in units:
        session.add(
            CustodyEvent(
                unit_id=unit.id,
                event_type=CustodyEventType.RECEIVED,
                to_location_id=unit.current_location_id,
            )
        )
    session.flush()


def operation(**changes):
    return {
        "key": str(uuid4()),
        "kind": "supply",
        "sku": "RAW",
        "quantity": 50,
        "required": 216,
        "cost": "550",
        "status": "confirmed",
        "available_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    } | changes


@pytest.mark.anyio
@pytest.mark.parametrize("available,quantity,required", [(200, 50, 216), (60, 50, 64)])
async def test_legacy_supply_resumes_from_real_stock_and_charges_once(
    db_session, available, quantity, required
):
    period, _, _, units = add_market_stock(db_session, quantity=1)
    op = operation(sku=units[0].lot.product.sku, quantity=quantity, required=required)
    state = ReplenishmentState(
        period_id=period.id,
        sku="KIT",
        goal={"quantity": 1},
        pending_operation=op,
        estimated_spend=Decimal(10),
    )
    db_session.add(state)
    db_session.commit()
    service = E3ReplenishmentService.__new__(E3ReplenishmentService)
    service.session, service.period = db_session, period
    producer = Mock(_available_units=Mock(return_value=[units[0]] * available))
    assert await service._resume_operation(state, producer) is False
    assert state.pending_operation is None
    assert state.estimated_spend == Decimal(560)
    assert state.goal["last_supply"]["basis"] == "usable_stock"
    assert await service._resume_operation(state, producer) is False
    assert state.estimated_spend == Decimal(560)
    assert units[0].status == "available"


@pytest.mark.anyio
@pytest.mark.parametrize("status,future", [("confirmed", True), ("uncertain", False)])
async def test_supply_waits_before_availability_or_without_confirmed_response(
    db_session, status, future
):
    period, _, _, units = add_market_stock(db_session, quantity=1)
    op = operation(status=status)
    if future:
        op["available_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    state = ReplenishmentState(
        period_id=period.id,
        sku="KIT",
        pending_operation=op,
        estimated_spend=0,
    )
    db_session.add(state)
    db_session.commit()
    service = E3ReplenishmentService.__new__(E3ReplenishmentService)
    service.session, service.period = db_session, period
    producer = Mock(_available_units=Mock(return_value=units * 100))
    assert await service._resume_operation(state, producer) is True
    assert state.pending_operation is not None
    assert state.estimated_spend == 0


@pytest.mark.parametrize("received", [1, 2, 3])
def test_receipt_ledger_survives_consumption_and_requires_unambiguous_quantity(
    db_session, received
):
    period, _, _, units = add_market_stock(db_session, quantity=4)
    sku = units[0].lot.product.sku
    receive(db_session, units[:1])
    baseline = supply_receipt_baseline(db_session, period, sku, inventory_complete=True)
    op = operation(sku=sku, quantity=2, **baseline)
    receive(db_session, units[1 : 1 + received])
    for unit in units[1:]:
        unit.status = "consumed"
    # Duplicate events for an existing unit must not count as another unit.
    receive(db_session, units[:1])
    basis = supply_resume_basis(db_session, period, op, usable_count=0)
    assert basis == ("receipt_ledger" if received == 2 else None)
    assert all(unit.status == "consumed" for unit in units[1:])


def test_incomplete_baseline_and_legacy_stock_shortage_cannot_prove_receipt(db_session):
    period, _, _, units = add_market_stock(db_session, quantity=2)
    sku = units[0].lot.product.sku
    baseline = supply_receipt_baseline(
        db_session, period, sku, inventory_complete=False
    )
    assert baseline == {}
    receive(db_session, units)
    assert (
        supply_resume_basis(
            db_session, period, operation(sku=sku, quantity=2), usable_count=1
        )
        is None
    )


@pytest.mark.parametrize("market_state", ["pending", "closed", "other_supply"])
def test_market_receipts_cannot_be_attributed_to_direct_supply(
    db_session, market_state
):
    period, _, _, units = add_market_stock(db_session, quantity=2)
    sku = units[0].lot.product.sku
    baseline = supply_receipt_baseline(db_session, period, sku, inventory_complete=True)
    if market_state == "pending":
        db_session.add(
            MarketOffer(
                period_id=period.id,
                side="buy",
                publisher_group_id=3,
                sku=sku,
                quantity=2,
                unit_price=1,
                status="open",
            )
        )
    elif market_state == "closed":
        db_session.add(
            MarketTrade(
                period_id=period.id,
                external_offer_id="offer",
                external_order_id="order",
                role=MarketRole.BUYER,
                counterparty_id="5",
                sku=sku,
                quantity=2,
                unit_price=1,
                closed_at=datetime.now(UTC),
            )
        )
    else:
        db_session.add(
            ReplenishmentState(
                period_id=period.id,
                sku="OTHER-KIT",
                pending_operation=operation(sku=sku, quantity=2),
            )
        )
    receive(db_session, units)
    assert (
        supply_resume_basis(
            db_session,
            period,
            operation(sku=sku, quantity=2, **baseline),
            usable_count=0,
        )
        is None
    )
