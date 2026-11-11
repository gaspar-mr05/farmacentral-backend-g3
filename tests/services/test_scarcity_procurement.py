from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.clients.trades import TradesHTTPError
from app.models import MarketOffer, ProcurementRequest
from app.schemas.procurement import ProcurementAction, ProcurementPlan
from app.services.supply.procurement import ScarcityProcurementService
from tests.services.test_procurement_planning import RAW, demand, offer, product
from tests.services.test_trade_custody import add_market_stock


def service(session, period, *, result=None):
    trades = Mock(
        list_offers=AsyncMock(return_value=[]),
        get_offer=AsyncMock(),
        create_offer=AsyncMock(),
        cancel_offer=AsyncMock(),
        accept_offer=AsyncMock(),
    )
    instance = ScarcityProcurementService(
        Mock(), trades, Mock(), Mock(), session, period
    )
    instance.preview = AsyncMock(
        return_value=result
        or ProcurementPlan(
            status="waiting",
            actions=[
                ProcurementAction(
                    kind="bid", sku=RAW, quantity=4, useful_quantity=4, cost=Decimal(8)
                )
            ],
        )
    )
    return instance


@pytest.mark.anyio
async def test_lost_creation_keeps_durable_intent_without_republishing(db_session):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    source.trades.create_offer.side_effect = TimeoutError("lost response")
    need = demand(sku=RAW, quantity=4)
    row = await source.ensure("shortage-1", need)
    assert row.status == "uncertain"
    local = db_session.get(MarketOffer, row.market_offer_id)
    assert local.status == "creation_uncertain"
    assert local.external_offer_id is None
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    restarted = service(db_session, period)
    row = await restarted.ensure("shortage-1", need)
    assert row.status == "uncertain"
    restarted.trades.create_offer.assert_not_awaited()
    assert len(db_session.scalars(select(ProcurementRequest)).all()) == 1


@pytest.mark.anyio
async def test_open_bid_is_a_wait_and_is_shared_instead_of_duplicated(db_session):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    opened = offer(RAW, 4, 2, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    need = demand(sku=RAW, quantity=4)
    row = await source.ensure("shortage-1", need)
    assert row.status == "waiting"
    await source.ensure("shortage-2", need)
    assert source.trades.create_offer.await_count == 1
    assert db_session.scalar(select(MarketOffer)).status == "open"


@pytest.mark.anyio
async def test_confirmed_rejection_can_be_replanned_without_unknown_mutation(
    db_session,
):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    source.trades.create_offer.side_effect = TradesHTTPError(422)
    row = await source.ensure("shortage-1", demand(sku=RAW, quantity=4))
    assert row.status == "waiting"
    assert row.market_offer_id is None
    assert db_session.scalar(select(MarketOffer)).status == "failed"


@pytest.mark.anyio
async def test_request_identity_is_not_reused_for_a_different_demand(db_session):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period, result=ProcurementPlan(status="ready"))
    await source.ensure("same-order", demand(sku=RAW, quantity=4))
    with pytest.raises(ValueError, match="different demand"):
        await source.ensure("same-order", demand(sku=RAW, quantity=5))


@pytest.mark.anyio
async def test_preview_does_not_spend_reserved_quarantined_or_committed_stock(
    db_session, monkeypatch
):
    period, storage, _, units = add_market_stock(db_session, quantity=3)
    sku = units[0].lot.product.sku
    units[0].status = "trade_reserved"
    units[1].status = "quarantined"
    db_session.commit()
    source = ScarcityProcurementService(
        Mock(),
        Mock(list_offers=AsyncMock(return_value=[])),
        Mock(),
        Mock(get_current_prices=AsyncMock(return_value=[])),
        db_session,
        period,
    )
    source.inventory.get_available_products = AsyncMock(
        return_value=[product(sku).model_dump(by_alias=True)]
    )
    source.inventory.get_spaces = AsyncMock(
        return_value=[
            {"_id": storage.code, "totalSpace": 100, "usedSpace": 3, "cold": True}
        ]
    )
    monkeypatch.setattr(
        "app.services.supply.procurement.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock())),
    )
    quote = await source.preview(demand(sku=sku, quantity=3))
    assert quote.actions[0].kind == "direct"
    assert quote.actions[0].quantity == 2
    source.trades.create_offer.assert_not_called()


@pytest.mark.anyio
async def test_purchase_is_confirmed_once_with_cost_and_supplier_trace(
    db_session, monkeypatch
):
    from app.models import MarketTrade
    from tests.services.test_trade_custody import TradesStub

    period, _, _, _ = add_market_stock(db_session)
    opened = offer(RAW, 4, 2)
    closed = opened.model_copy(
        update={
            "status": "closed",
            "counterparty_group": 3,
            "order_id": "b" * 24,
            "closed_at": datetime.now(UTC),
        }
    )
    quote = ProcurementPlan(
        status="market",
        cost=8,
        actions=[
            ProcurementAction(
                kind="accept",
                sku=RAW,
                quantity=4,
                useful_quantity=4,
                cost=8,
                offer_id=opened.id,
            )
        ],
    )
    source = service(db_session, period, result=quote)
    source.trades.get_offer.side_effect = [opened, closed]
    source.trades.accept_offer.return_value = closed
    source.custody._purchase_orders = TradesStub(closed)
    synchronize = AsyncMock()
    monkeypatch.setattr(
        "app.services.supply.procurement.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )
    need = demand(sku=RAW, quantity=4)
    row = await source.ensure("one-real-purchase", need)
    assert row.status == "acquired"
    assert row.decision["acquisition"] == {
        "sku": RAW,
        "quantity": 4,
        "cost": "8",
        "counterparty": "5",
        "order_id": "b" * 24,
    }
    await source.ensure("one-real-purchase", need)
    assert source.trades.accept_offer.await_count == 1
    assert len(db_session.scalars(select(MarketTrade)).all()) == 1
    synchronize.assert_awaited_once()


@pytest.mark.anyio
async def test_better_sell_offer_cancels_existing_bid_before_replanning(db_session):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    opened = offer(RAW, 4, 2, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    need = demand(sku=RAW, quantity=4)
    row = await source.ensure("bid-then-sell", need)
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    source.preview.return_value = ProcurementPlan(
        status="market",
        cost=4,
        actions=[
            ProcurementAction(
                kind="accept",
                sku=RAW,
                quantity=4,
                useful_quantity=4,
                cost=4,
                offer_id="d" * 24,
            )
        ],
    )
    source.trades.get_offer.side_effect = [
        opened,
        opened.model_copy(update={"status": "cancelled"}),
    ]
    await source.ensure("bid-then-sell", need)
    source.trades.cancel_offer.assert_awaited_once_with(opened.id)
    source.trades.accept_offer.assert_not_awaited()
    assert row.market_offer_id is None
    assert row.status == "waiting"


@pytest.mark.anyio
async def test_accepted_unassigned_commitment_is_deducted_from_usable_stock(
    db_session, monkeypatch
):
    from app.models import Order, OrderItem, OrderSource, OrderStatus

    period, storage, _, units = add_market_stock(db_session, quantity=3)
    sku = units[0].lot.product.sku
    order = Order(
        buyer_name="Committed customer",
        source=OrderSource.SFTP,
        status=OrderStatus.ACCEPTED,
        total=1,
        items=[OrderItem(sku=sku, quantity=2, unit_price=1)],
    )
    db_session.add(order)
    db_session.commit()
    inventory = Mock(
        get_available_products=AsyncMock(
            return_value=[product(sku).model_dump(by_alias=True)]
        ),
        get_spaces=AsyncMock(
            return_value=[
                {"_id": storage.code, "totalSpace": 100, "usedSpace": 3, "cold": True}
            ]
        ),
    )
    source = ScarcityProcurementService(
        inventory,
        Mock(list_offers=AsyncMock(return_value=[])),
        Mock(),
        Mock(get_current_prices=AsyncMock(return_value=[])),
        db_session,
        period,
    )
    monkeypatch.setattr(
        "app.services.supply.procurement.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock())),
    )
    quote = await source.preview(demand(sku=sku, quantity=3))
    assert quote.actions[0].quantity == 2


@pytest.mark.anyio
async def test_cancel_race_recognizes_purchase_instead_of_accepting_another_offer(
    db_session, monkeypatch
):
    from tests.services.test_trade_custody import TradesStub

    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    opened = offer(RAW, 4, 2, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    need = demand(sku=RAW, quantity=4)
    row = await source.ensure("cancel-race", need)
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    closed = opened.model_copy(
        update={
            "status": "closed",
            "counterparty_group": 5,
            "closed_at": datetime.now(UTC),
            "order_id": "e" * 24,
        }
    )

    class SupplierOrders(TradesStub):
        async def get_order(self, order_id):
            return (await super().get_order(order_id)).model_copy(
                update={"supplier_id": "5", "customer_id": "3"}
            )

    source.custody._purchase_orders = SupplierOrders(closed)
    source.preview.return_value = ProcurementPlan(
        status="market",
        cost=4,
        actions=[
            ProcurementAction(
                kind="accept",
                sku=RAW,
                quantity=4,
                useful_quantity=4,
                cost=4,
                offer_id="d" * 24,
            )
        ],
    )
    source.trades.get_offer.side_effect = [opened, closed]
    monkeypatch.setattr(
        "app.services.supply.procurement.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock())),
    )
    await source.ensure("cancel-race", need)
    assert row.status == "acquired"
    assert row.decision["acquisition"]["order_id"] == "e" * 24
    source.trades.accept_offer.assert_not_awaited()
