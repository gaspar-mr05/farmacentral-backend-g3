from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
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
from app.schemas.purchase_orders import PurchaseOrder
from app.schemas.trades import TradeOffer, TradeOfferCreate
from app.schemas.units import UnitData
from app.services.traceability import TraceabilityService
from app.services.trades import TradeCustodyService
from app.services.trades.custody import InvalidTradeStateError
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration


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

    async def get_order(self, order_id):
        offer = self.response
        return PurchaseOrder.model_validate(
            {
                "id": order_id,
                "cliente": str(offer.publisher_group if offer.side == "buy" else 3),
                "proveedor": str(3 if offer.side == "buy" else offer.publisher_group),
                "sku": offer.sku,
                "cantidad": offer.quantity,
                "despachado": offer.quantity,
                "precioUnitario": offer.unit_price,
                "estado": "cumplida",
                "historial": [],
                "creada": offer.created_at,
                "actualizada": offer.closed_at,
                "vencimiento": offer.expires_at,
                "facturado": False,
            }
        )

    async def accept_offer(self, offer_id: str):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_service(session, **kwargs):
    return TradeCustodyService(session, purchase_orders=kwargs["trades"], **kwargs)


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
        state=status,
        counterparty="3" if status == "closed" else None,
        closedAt=now if status == "closed" else None,
        orderId=order_id,
        publisher=str(publisher_group),
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
    service = make_service(
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
    service = make_service(
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
    service = make_service(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=TradesStub(accepted),
        period=period,
    )

    _, trade = await service.accept_buy(
        accepted.model_copy(update={"status": "open", "closed_at": None})
    )

    assert (
        db_session.scalars(
            select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
        ).all()
        == []
    )
    assert all(unit.status == "trade_reserved" for unit in units[:2])
    service.record_trade_units(trade.id, [unit.external_unit_id for unit in units[:2]])

    db_session.expire_all()
    assert trade.role == MarketRole.SELLER
    assert trade.counterparty_id == "5"
    events = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
    ).all()
    assert {event.unit_id for event in events} == {units[0].id, units[1].id}
    assert {event.event_type for event in events} == {CustodyEventType.DISPATCHED}
    assert all(event.occurred_at == trade.closed_at for event in events)
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
    service = make_service(
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


@pytest.mark.anyio
async def test_quarantine_before_movement_prevents_market_call(db_session):

    period, _, dispatch, units = add_market_stock(db_session, quantity=2)

    class RecalledStock(FarmaCentralStub):
        async def get_spaces(self):
            units[0].status = "quarantined"
            db_session.commit()
            return await super().get_spaces()

    fc = RecalledStock(dispatch.code)
    trades = Mock(accept_offer=AsyncMock())
    offer = official_offer(sku=units[0].lot.product.sku, side="buy", publisher_group=5)
    service = make_service(db_session, farma_central=fc, trades=trades, period=period)
    with pytest.raises(InvalidTradeStateError, match="blocked or changed"):
        await service.accept_buy(offer)
    trades.accept_offer.assert_not_awaited()
    assert fc.movements == []
    assert units[0].status == "quarantined"


@pytest.mark.anyio
async def test_quarantine_after_movement_prevents_publication(db_session):

    period, _, dispatch, units = add_market_stock(db_session, quantity=2)

    class RecalledStock(FarmaCentralStub):
        async def move_product(self, unit_id, location_code):
            await super().move_product(unit_id, location_code)
            if unit_id == units[1].external_unit_id:
                units[0].status = "quarantined"
                db_session.commit()

    trades = Mock(create_offer=AsyncMock())
    service = make_service(
        db_session,
        farma_central=RecalledStock(dispatch.code),
        trades=trades,
        period=period,
    )
    with pytest.raises(InvalidTradeStateError):
        await service.publish_sell(
            TradeOfferCreate(
                side="sell", sku=units[0].lot.product.sku, quantity=2, unitPrice="6.25"
            ),
            current_unit_price=Decimal(5),
        )
    trades.create_offer.assert_not_awaited()
    assert units[0].status == "quarantined"


@pytest.mark.anyio
async def test_actual_transfer_ids_supersede_unconfirmed_selection(db_session):
    period, _, dispatch, units = add_market_stock(db_session, quantity=4)
    accepted = official_offer(
        sku=units[0].lot.product.sku,
        side="buy",
        status="closed",
        order_id="b" * 24,
        publisher_group=5,
    )
    service = make_service(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=TradesStub(accepted),
        period=period,
    )
    _, trade = await service.accept_buy(
        accepted.model_copy(update={"status": "open", "closed_at": None})
    )
    assert (
        db_session.scalar(
            select(CustodyEvent.id).where(CustodyEvent.market_trade_id == trade.id)
        )
        is None
    )
    service.record_trade_units(trade.id, [unit.external_unit_id for unit in units[2:]])
    service.record_trade_units(trade.id, [unit.external_unit_id for unit in units[2:]])
    events = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
    ).all()
    assert {event.unit_id for event in events} == {unit.id for unit in units[2:]}
    assert all(unit.status == "available" for unit in units[:2])
    assert all(unit.status == "dispatched" for unit in units[2:])


@pytest.mark.anyio
async def test_movement_timeout_keeps_reservations_until_reconciled(db_session):
    period, _, dispatch, units = add_market_stock(db_session, quantity=2)

    class LostMovement(FarmaCentralStub):
        async def move_product(self, *args):
            raise TimeoutError("response lost")

    service = make_service(
        db_session,
        farma_central=LostMovement(dispatch.code),
        trades=TradesStub(RuntimeError()),
        period=period,
    )
    with pytest.raises(TimeoutError):
        await service.publish_sell(
            TradeOfferCreate(
                side="sell", sku=units[0].lot.product.sku, quantity=2, unitPrice="6.25"
            ),
            current_unit_price=Decimal(5),
        )
    offer = db_session.scalar(select(MarketOffer))
    assert offer.status == "movement_uncertain"
    assert all(reservation.released_at is None for reservation in offer.reservations)
    assert all(unit.status == "trade_reserved" for unit in units)


@pytest.mark.anyio
async def test_market_request_keeps_unit_locks_against_concurrent_recall(
    migration_database,
):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine, expire_on_commit=False) as primary:
        period, _, dispatch, units = add_market_stock(primary, quantity=2)
        accepted = official_offer(
            sku=units[0].lot.product.sku,
            side="buy",
            status="closed",
            order_id="b" * 24,
            publisher_group=5,
        )

        class CheckLocks(TradesStub):
            async def accept_offer(self, offer_id):
                with Session(engine) as contender:
                    contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    with pytest.raises(OperationalError):
                        contender.execute(
                            update(Unit)
                            .where(Unit.id == units[0].id)
                            .values(status="quarantined")
                        )
                    contender.rollback()
                return accepted

        service = make_service(
            primary,
            farma_central=FarmaCentralStub(dispatch.code),
            trades=CheckLocks(accepted),
            period=period,
        )
        await service.accept_buy(
            accepted.model_copy(update={"status": "open", "closed_at": None})
        )
        assert units[0].status == "trade_reserved"


@pytest.mark.anyio
async def test_reduced_official_delivery_uses_order_quantity_and_closure_time(
    db_session,
):
    period, _, dispatch, units = add_market_stock(db_session, quantity=3)
    accepted = official_offer(
        sku=units[0].lot.product.sku,
        side="buy",
        status="closed",
        order_id="b" * 24,
        publisher_group=5,
    )
    closed_at = datetime.now(UTC) - timedelta(minutes=5)
    accepted = accepted.model_copy(update={"closed_at": closed_at})

    class ReducedDelivery(TradesStub):
        async def get_order(self, order_id):
            order = await super().get_order(order_id)
            return order.model_copy(update={"quantity": 1, "dispatched_quantity": 1})

    service = make_service(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=ReducedDelivery(accepted),
        period=period,
    )
    _, trade = await service.accept_buy(accepted.model_copy(update={"status": "open"}))
    assert trade.quantity == 1
    assert trade.total == Decimal("6.25")
    assert trade.closed_at == closed_at
    service.record_trade_units(trade.id, [units[0].external_unit_id])
    assert units[0].status == "dispatched"
    assert units[1].status == "available"
    assert all(
        r.released_at is not None
        for r in db_session.scalars(select(MarketOfferReservation))
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid", ["wrong_buyer", "wrong_state", "wrong_quantity", "unavailable"]
)
async def test_unconfirmed_order_keeps_closure_and_reservations_without_trade(
    db_session, invalid
):
    period, _, dispatch, units = add_market_stock(db_session, quantity=2)
    accepted = official_offer(
        sku=units[0].lot.product.sku,
        side="buy",
        status="closed",
        order_id="b" * 24,
        publisher_group=5,
    )

    class InvalidOrder(TradesStub):
        async def get_order(self, order_id):
            if invalid == "unavailable":
                raise TimeoutError("Order service unavailable")
            order = await super().get_order(order_id)
            changes = {
                "wrong_buyer": {"customer_id": "9"},
                "wrong_state": {"state": "aceptada"},
                "wrong_quantity": {"quantity": 3, "dispatched_quantity": 3},
            }
            return order.model_copy(update=changes[invalid])

    service = make_service(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=InvalidOrder(accepted),
        period=period,
    )
    with pytest.raises((InvalidTradeStateError, TimeoutError)):
        await service.accept_buy(accepted.model_copy(update={"status": "open"}))
    db_session.expire_all()
    offer = db_session.scalar(select(MarketOffer))
    assert offer.status == "closed"
    assert offer.external_order_id == accepted.order_id
    assert db_session.scalar(select(MarketTrade)) is None
    assert all(unit.status == "trade_reserved" for unit in units)


@pytest.mark.anyio
async def test_accept_sell_confirms_buyer_role_with_official_order(db_session):
    period, _, dispatch, units = add_market_stock(db_session, quantity=2)
    accepted = official_offer(
        sku=units[0].lot.product.sku,
        side="sell",
        status="closed",
        order_id="b" * 24,
        publisher_group=5,
    )
    service = make_service(
        db_session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=TradesStub(accepted),
        period=period,
    )
    _, trade = await service.accept_sell(accepted.model_copy(update={"status": "open"}))
    assert trade.role == MarketRole.BUYER
    assert trade.counterparty_id == "5"
    assert trade.closed_at == accepted.closed_at
    assert db_session.scalar(select(MarketOfferReservation)) is None
