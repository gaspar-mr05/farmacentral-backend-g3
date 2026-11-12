from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.trades import TradesTimeoutError
from app.models import (
    BusinessPeriod,
    CustodyEvent,
    CustodyEventType,
    MarketOffer,
    MarketOfferReservation,
    MarketRole,
    MarketTrade,
    Recall,
    RecallStatus,
)
from app.queries.recalls import block_recalled_stock, findings_changed
from app.schemas.trades import TradeOffer
from app.services.recalls import (
    RecallInvestigationError,
    RecallInvestigationService,
    RecallQueryService,
    RecallResponseError,
    RecallResponseService,
)
from app.services.recalls.market import RecallMarketPendingError
from app.services.trades import TradeCustodyService
from tests.services.test_recalls import recall_database as recall_database
from tests.support.farma_central import catalog_product
from tests.support.orders import make_purchase_order
from tests.support.recalls import RecallInventory


def period(session, inventory):
    row = BusinessPeriod(
        code=inventory.suffix,
        environment="dev",
        group_id=3,
        starts_at=inventory.now - timedelta(hours=1),
    )
    session.add(row)
    session.flush()
    return row


def transfer(session, inventory, unit, period, role):
    trade = MarketTrade(
        period_id=period.id,
        external_offer_id=uuid4().hex[:24],
        external_order_id=uuid4().hex[:24],
        role=role,
        counterparty_id="4",
        sku=unit.lot.product.sku,
        quantity=1,
        unit_price=Decimal("100"),
        closed_at=inventory.now,
    )
    session.add(trade)
    session.flush()
    session.add(
        CustodyEvent(
            unit_id=unit.id,
            market_trade_id=trade.id,
            event_type=CustodyEventType.RECEIVED
            if role == MarketRole.BUYER
            else CustodyEventType.DISPATCHED,
            occurred_at=inventory.now,
        )
    )
    session.flush()
    return trade


def inventory_client(inventory, unit=None, *, batch=True, count=1):
    client = AsyncMock()
    client.get_available_products.return_value = [
        catalog_product(p.sku, p.name) for p in inventory.products.values()
    ]
    client.get_spaces.return_value = [
        {"_id": location.code, "quarantine": role == "quarantine"}
        for role, location in inventory.locations.items()
    ]

    async def listing(code):
        return (
            [{"sku": unit.lot.product.sku, "quantity": count}]
            if unit and code == unit.current_location.code
            else []
        )

    client.get_space_inventory.side_effect = listing
    client.get_space_products.return_value = (
        []
        if unit is None
        else [
            {
                "_id": unit.external_unit_id,
                "sku": unit.lot.product.sku,
                "store": unit.current_location.code,
                "expiresAt": inventory.expiry.isoformat(),
                **({"batch": unit.lot.external_lot_id} if batch else {}),
            }
        ]
    )
    return client


@pytest.mark.anyio
async def test_market_purchase_production_and_end_customer_delivery(recall_database):
    with Session(recall_database) as session:
        g = RecallInventory(session)
        root = g.lot("bought", "RAW")
        raw = g.unit("input", root, status="consumed")
        transfer(session, g, raw, period(session, g), MarketRole.BUYER)
        derived = g.lot("produced")
        g.link(derived, root, consumed_unit=raw)
        stock = g.unit("stock", derived)
        delivered = g.unit("customer", derived)
        order = g.order((delivered, True))
        recall = g.recall(root)
        session.commit()
        client = inventory_client(g)
        result = await RecallResponseService(client, session).respond(recall.recall_id)
        report = client.report_recall.await_args.args[1]
        assert result.quarantined_units == 1 and result.notified_orders == 1
        assert stock.status == "quarantined"
        assert report.derived_batches[0].batch == derived.external_lot_id
        assert report.deliveries[0].order_id == order.external_order_id
        assert report.deliveries[0].product_ids == [delivered.external_unit_id]
        assert recall.reported_at < recall.issued_at + timedelta(minutes=30)


@pytest.mark.anyio
async def test_market_resale_uses_official_order_without_customer_notification(
    recall_database,
):
    with Session(recall_database) as session:
        g = RecallInventory(session)
        root = g.lot("bought")
        sold = g.unit("resold", root, status="dispatched")
        p = period(session, g)
        transfer(session, g, sold, p, MarketRole.BUYER)
        trade = transfer(session, g, sold, p, MarketRole.SELLER)
        recall = g.recall(root)
        session.commit()
        client = inventory_client(g)
        result = await RecallResponseService(client, session).respond(recall.recall_id)
        assert result.quarantined_units == result.notified_orders == 0
        client.move_product.assert_not_awaited()
        client.notify_recall.assert_not_awaited()
        report = client.report_recall.await_args.args[1]
        assert report.deliveries[0].order_id == trade.external_order_id
        assert report.deliveries[0].product_ids == [sold.external_unit_id]
        assert report.in_stock == []
        detail = RecallQueryService(session).get_recall(recall.recall_id)
        assert detail.market_deliveries[0].external_order_id == trade.external_order_id
        assert detail.market_deliveries[0].external_unit_id == sold.external_unit_id
        assert not findings_changed(session, recall)
        recall_id = recall.id
    with Session(recall_database) as session:
        recall = session.get(Recall, recall_id)
        assert (
            recall.investigation_evidence["market_deliveries"][0]["external_order_id"]
            == trade.external_order_id
        )


@pytest.mark.anyio
async def test_unrelated_batch_requires_inventory_and_history_and_moves_nothing(
    recall_database,
):
    with Session(recall_database) as session:
        g = RecallInventory(session)
        root = g.lot("own")
        own = g.unit("own", root)
        session.add(CustodyEvent(unit_id=own.id, event_type=CustodyEventType.RECEIVED))
        recall = g.recall(root)
        recall.batch = "foreign-official-batch"
        session.commit()
        client = inventory_client(g, own)
        result = await RecallResponseService(client, session).respond(recall.recall_id)
        assert result.quarantined_units == result.notified_orders == 0
        assert client.report_recall.await_args.args[1].model_dump(by_alias=True) == {
            "inStock": [],
            "derivedBatches": [],
            "deliveries": [],
        }
        assert own.status == "available"
        client.move_product.assert_not_awaited()
        client.notify_recall.assert_not_awaited()
        assert recall.status == RecallStatus.COMPLETED and recall.origin_lot_id is None
        assert recall.investigation_evidence["ownership"]["inventoryComplete"]
        assert not findings_changed(session, recall)
        detail = RecallQueryService(session).get_recall(recall.recall_id)
        assert detail.units == [] and detail.ownership["kind"] == "unaffected"
        root.external_lot_id = recall.batch
        session.commit()
        assert findings_changed(session, recall)
        block_recalled_stock(session)
        assert own.status == "recall_pending"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "problem", ["provisional", "truncated", "missing_custody", "missing_trade_units"]
)
async def test_missing_batch_is_not_proof_of_no_affected_units(
    recall_database, problem
):
    with Session(recall_database) as session:
        g = RecallInventory(session)
        root = g.lot("own")
        own = g.unit("own", root)
        if problem != "missing_custody":
            session.add(
                CustodyEvent(unit_id=own.id, event_type=CustodyEventType.RECEIVED)
            )
        recall = g.recall(root)
        recall.batch = "unknown-official-batch"
        if problem == "provisional":
            root.external_lot_id = "unreported:" + own.external_unit_id
        if problem == "missing_trade_units":
            transfer(session, g, own, period(session, g), MarketRole.BUYER).quantity = 2
        session.commit()
        client = inventory_client(
            g,
            own,
            batch=problem != "provisional",
            count=201 if problem == "truncated" else 1,
        )
        with pytest.raises(
            RecallInvestigationError, match="incomplete|not synchronized"
        ):
            await RecallResponseService(client, session).respond(recall.recall_id)
        client.report_recall.assert_not_awaited()
        client.move_product.assert_not_awaited()
        assert recall.reported_at is None


def reserve(session, g, root):
    unit = g.unit("reserved", root, status="trade_reserved")
    p = period(session, g)
    offer = MarketOffer(
        period_id=p.id,
        external_offer_id="a" * 24,
        side="sell",
        publisher_group_id=3,
        sku=root.product.sku,
        quantity=1,
        unit_price=100,
        status="open",
    )
    offer.reservations = [MarketOfferReservation(unit=unit, reserved_at=g.now)]
    session.add(offer)
    session.flush()
    return unit, offer


def official(g, offer, state):
    return TradeOffer(
        id=offer.external_offer_id,
        side="sell",
        sku=offer.sku,
        quantity=1,
        unitPrice=100,
        state=state,
        publisher=3,
        counterparty=4 if state == "closed" else None,
        orderId="b" * 24 if state == "closed" else None,
        closedAt=g.now if state == "closed" else None,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_timeout", [False, True])
async def test_market_reservation_is_released_only_after_confirmed_cancellation(
    recall_database, cancel_timeout
):
    with Session(recall_database) as session:
        g = RecallInventory(session)
        root = g.lot("affected")
        unit, offer = reserve(session, g, root)
        recall = g.recall(root)
        session.commit()
        client = inventory_client(g)
        trades, orders = AsyncMock(), AsyncMock()
        trades.get_offer.side_effect = [
            official(g, offer, "open"),
            official(g, offer, "cancelled"),
        ]
        if cancel_timeout:
            trades.cancel_offer.side_effect = TradesTimeoutError("lost cancellation")
        service = RecallResponseService(
            client, session, trades=trades, purchase_orders=orders
        )
        if cancel_timeout:
            with pytest.raises(TradesTimeoutError):
                await service.respond(recall.recall_id)
            assert unit.status == "recall_pending"
            assert offer.reservations[0].released_at is None
            client.move_product.assert_not_awaited()
            trades.cancel_offer.side_effect = None
            # GET proves that the previous POST already cancelled it.
            await service.respond(recall.recall_id)
        else:
            await service.respond(recall.recall_id)
        trades.cancel_offer.assert_awaited_once()
        assert offer.reservations[0].released_at is not None
        assert unit.status == "quarantined"
        client.move_product.assert_awaited_once()


@pytest.mark.anyio
async def test_market_close_winning_cancel_waits_for_official_unit_evidence(
    recall_database,
):
    with Session(recall_database) as session:
        g = RecallInventory(session)
        root = g.lot("affected")
        unit, offer = reserve(session, g, root)
        recall = g.recall(root)
        session.commit()
        client = inventory_client(g)
        trades, orders = AsyncMock(), AsyncMock()
        closed = official(g, offer, "closed")
        trades.get_offer.side_effect = [official(g, offer, "open"), closed]
        orders.get_order.return_value = make_purchase_order(
            id=closed.order_id,
            cliente="4",
            proveedor="3",
            sku=offer.sku,
            cantidad=1,
            despachado=1,
            precioUnitario=100,
            estado="cumplida",
        )
        service = RecallResponseService(
            client, session, trades=trades, purchase_orders=orders
        )
        with pytest.raises(
            RecallMarketPendingError, match="official transferred unit IDs"
        ):
            await service.respond(recall.recall_id)
        client.move_product.assert_not_awaited()
        client.report_recall.assert_not_awaited()
        trade = session.scalar(select(MarketTrade))
        custody = TradeCustodyService(
            session,
            farma_central=client,
            trades=trades,
            purchase_orders=orders,
            period=offer.period,
        )
        custody.record_trade_units(trade.id, [unit.external_unit_id])
        await service.respond(recall.recall_id)
        assert unit.status == "dispatched"
        client.move_product.assert_not_awaited()
        client.notify_recall.assert_not_awaited()
        assert (
            client.report_recall.await_args.args[1].deliveries[0].order_id
            == closed.order_id
        )
        assert client.report_recall.await_args.args[1].in_stock == []


@pytest.mark.anyio
async def test_payload_cannot_extend_the_thirty_minute_window(recall_database):
    with Session(recall_database) as session:
        g = RecallInventory(session)
        root = g.lot("affected")
        unit = g.unit("stock", root)
        recall = g.recall(root)
        recall.issued_at = g.now - timedelta(minutes=31)
        session.commit()
        client = inventory_client(g)
        with pytest.raises(RecallResponseError, match="deadline"):
            await RecallResponseService(client, session).respond(recall.recall_id)
        assert unit.status == "available"
        client.get_spaces.assert_not_awaited()
        client.move_product.assert_not_awaited()
        client.report_recall.assert_not_awaited()


def test_public_recall_detail_preserves_official_market_ids(api_client, db_session):
    g = RecallInventory(db_session)
    root = g.lot("sold")
    unit = g.unit("resold", root, status="dispatched")
    trade = transfer(db_session, g, unit, period(db_session, g), MarketRole.SELLER)
    recall = g.recall(root)
    db_session.commit()
    RecallInvestigationService(db_session).investigate(recall.recall_id)
    response = api_client.get(f"/api/recalls/{recall.recall_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["units"] == []
    assert body["ownership"]["kind"] == "affected"
    assert body["market_deliveries"][0]["external_order_id"] == trade.external_order_id
    assert body["market_deliveries"][0]["external_unit_id"] == unit.external_unit_id
