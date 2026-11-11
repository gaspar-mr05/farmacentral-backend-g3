from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.units import upsert_units
from app.models import (
    BusinessPeriod,
    CustodyEvent,
    CustodyEventType,
    Location,
    Lot,
    LotOrigin,
    MarketOffer,
    MarketOfferReservation,
    MarketRole,
    MarketTrade,
    Product,
    ProductCategory,
    Unit,
)
from app.schemas.trades import TradeOffer, TradeOfferCreate
from app.schemas.units import UnitData
from app.services.traceability import TraceabilityService
from app.services.trades import TradeCustodyService


class FarmaCentralStub:
    def __init__(self, dispatch_code: str) -> None:
        self.dispatch_code = dispatch_code
        self.movements: list[tuple[str, str]] = []

    async def get_spaces(self):
        return [{"_id": self.dispatch_code, "checkOut": True}]

    async def move_product(self, unit_id: str, location_code: str) -> None:
        self.movements.append((unit_id, location_code))


class TradesStub:
    def __init__(self, response: TradeOffer | Exception) -> None:
        self.response = response

    async def create_offer(self, request, *, current_unit_price):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def accept_offer(self, offer_id: str):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def add_market_stock(
    session: Session, *, quantity: int = 3
) -> tuple[BusinessPeriod, Location, Location, list[Unit]]:
    suffix = uuid4().hex
    period = BusinessPeriod(
        environment="dev",
        code=f"trade-{suffix}",
        group_id=3,
        starts_at=datetime.now(UTC) - timedelta(hours=1),
    )
    product = Product(
        sku=f"TRADE-SKU-{suffix}",
        name="Trade stock",
        category=ProductCategory.INSUMO,
        batch_size=1,
        requires_refrigeration=True,
    )
    storage = Location(
        code=f"storage-{suffix}",
        name="Cold storage",
        is_refrigerated=True,
        is_sellable=True,
    )
    dispatch = Location(
        code=f"dispatch-{suffix}",
        name="Dispatch",
        is_refrigerated=True,
        is_sellable=False,
    )
    session.add_all([period, product, storage, dispatch])
    session.flush()
    lot = Lot(
        external_lot_id=f"lot-{suffix}",
        product_id=product.id,
        expires_at=datetime.now(UTC) + timedelta(days=90),
        origin=LotOrigin.FARMA_CENTRAL,
    )
    session.add(lot)
    session.flush()
    units = [
        Unit(
            external_unit_id=f"unit-{suffix}-{index}",
            lot_id=lot.id,
            current_location_id=storage.id,
            status="available",
            effective_expires_at=datetime.now(UTC) + timedelta(days=days),
        )
        for index, days in enumerate(range(10, 10 + quantity))
    ]
    session.add_all(units)
    session.commit()
    return period, storage, dispatch, units


def official_offer(
    *,
    sku: str,
    side: str = "sell",
    status: str = "open",
    order_id: str | None = None,
    publisher_group: int = 3,
    quantity: int = 2,
) -> TradeOffer:
    now = datetime.now(UTC)
    return TradeOffer(
        id="a" * 24,
        side=side,
        sku=sku,
        quantity=quantity,
        unitPrice="6.25",
        status=status,
        orderId=order_id,
        publisherGroup=publisher_group,
        createdAt=now,
        expiresAt=now + timedelta(hours=1),
    )


@pytest.mark.anyio
async def test_publish_sell_reserves_fefo_stock_before_opening_offer(
    db_session: Session,
) -> None:
    period, _, dispatch, units = add_market_stock(db_session)
    response = official_offer(sku=units[0].lot.product.sku)
    farma_central = FarmaCentralStub(dispatch.code)
    service = TradeCustodyService(
        db_session,
        farma_central=farma_central,
        trades=TradesStub(response),
        period=period,
    )

    result = await service.publish_sell(
        TradeOfferCreate(side="sell", sku=response.sku, quantity=2, unitPrice="6.25"),
        current_unit_price=Decimal("5"),
    )

    assert result.id == response.id
    assert farma_central.movements == [
        (units[0].external_unit_id, dispatch.code),
        (units[1].external_unit_id, dispatch.code),
    ]
    upsert_units(
        db_session,
        [
            UnitData(
                external_unit_id=units[0].external_unit_id,
                lot_external_id=units[0].lot.external_lot_id,
                location_code=dispatch.code,
                status="available",
                effective_expires_at=units[0].effective_expires_at,
            )
        ],
        {units[0].lot.external_lot_id: units[0].lot},
        {dispatch.code: dispatch},
        incomplete_groups={(dispatch.code, response.sku)},
    )
    db_session.expire_all()
    offer = db_session.scalar(select(MarketOffer))
    assert offer.external_offer_id == response.id
    assert offer.status == "open"
    assert {reservation.unit_id for reservation in offer.reservations} == {
        units[0].id,
        units[1].id,
    }
    assert all(reservation.released_at is None for reservation in offer.reservations)
    assert db_session.get(Unit, units[0].id).status == "trade_reserved"
    assert db_session.get(Unit, units[1].id).status == "trade_reserved"
    assert db_session.get(Unit, units[2].id).status == "available"


@pytest.mark.anyio
async def test_uncertain_creation_keeps_units_reserved(db_session: Session) -> None:
    period, _, dispatch, units = add_market_stock(db_session)
    service = TradeCustodyService(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=TradesStub(RuntimeError("response lost")),
        period=period,
    )

    with pytest.raises(RuntimeError, match="response lost"):
        await service.publish_sell(
            TradeOfferCreate(
                side="sell",
                sku=units[0].lot.product.sku,
                quantity=2,
                unitPrice="6.25",
            ),
            current_unit_price=Decimal("5"),
        )

    db_session.expire_all()
    offer = db_session.scalar(select(MarketOffer))
    assert offer.status == "creation_uncertain"
    assert all(reservation.released_at is None for reservation in offer.reservations)
    assert db_session.get(Unit, units[0].id).status == "trade_reserved"
    assert db_session.get(Unit, units[1].id).status == "trade_reserved"


@pytest.mark.anyio
async def test_accept_buy_records_sale_without_dispatching_again(
    db_session: Session,
) -> None:
    period, _, dispatch, units = add_market_stock(db_session)
    accepted = official_offer(
        sku=units[0].lot.product.sku,
        side="buy",
        status="closed",
        order_id="b" * 24,
        publisher_group=5,
    )
    service = TradeCustodyService(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=TradesStub(accepted),
        period=period,
    )

    _, trade = await service.accept_buy(accepted)

    db_session.expire_all()
    assert trade.role == MarketRole.SELLER
    assert trade.counterparty_id == "5"
    events = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
    ).all()
    assert {event.unit_id for event in events} == {units[0].id, units[1].id}
    assert {event.event_type for event in events} == {CustodyEventType.DISPATCHED}
    assert db_session.get(Unit, units[0].id).status == "dispatched"
    assert db_session.get(Unit, units[1].id).status == "dispatched"
    assert all(
        reservation.released_at is not None
        for reservation in db_session.scalars(select(MarketOfferReservation))
    )
    traceability = TraceabilityService(db_session).get_traceability(units[0].lot_id)
    assert {delivery.order_id for delivery in traceability.deliveries} == {
        accepted.order_id
    }
    assert traceability.market_transfers[0].counterparty_id == "5"


def test_purchase_receipt_preserves_lot_and_supports_reentry(
    db_session: Session,
) -> None:
    period, _, dispatch, units = add_market_stock(db_session, quantity=1)
    unit = units[0]
    original_lot_id = unit.lot_id
    unit.status = "dispatched"
    unit.current_location_id = dispatch.id
    trade = MarketTrade(
        period_id=period.id,
        external_offer_id="c" * 24,
        external_order_id="d" * 24,
        role=MarketRole.BUYER,
        counterparty_id="6",
        sku=unit.lot.product.sku,
        quantity=1,
        unit_price=Decimal("7.5"),
        closed_at=datetime.now(UTC),
    )
    db_session.add(trade)
    db_session.commit()
    service = TradeCustodyService(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=TradesStub(RuntimeError()),
        period=period,
    )

    service.record_trade_units(trade.id, [unit.external_unit_id])
    service.record_trade_units(trade.id, [unit.external_unit_id])

    db_session.expire_all()
    reentered = db_session.get(Unit, unit.id)
    assert reentered.status == "available"
    assert reentered.lot_id == original_lot_id
    event = db_session.scalar(
        select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
    )
    assert event.event_type == CustodyEventType.RECEIVED
    assert event.to_location_id == dispatch.id
