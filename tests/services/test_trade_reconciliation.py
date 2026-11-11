from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.clients.trades import TradesHTTPError
from app.models import BillingTask, CustodyEvent, MarketOffer, MarketRole, MarketTrade
from app.schemas.trades import TradeOfferCreate
from app.services.trades.reconciliation import TradeReconciliationService
from tests.services.test_trade_custody import (
    FarmaCentralStub,
    TradesStub,
    add_market_stock,
    make_service,
    official_offer,
)


def reconciler(session, period, dispatch, official, *, order_source=None):
    trades = Mock(get_offer=AsyncMock(return_value=official))
    orders = order_source or TradesStub(official)
    inventory = FarmaCentralStub(dispatch.code)
    return TradeReconciliationService(trades, orders, inventory, session, period)


async def published_sale(session):
    period, _, dispatch, units = add_market_stock(session)
    opened = official_offer(sku=units[0].lot.product.sku)
    custody = make_service(
        session,
        farma_central=FarmaCentralStub(dispatch.code),
        trades=TradesStub(opened),
        period=period,
    )
    await custody.publish_sell(
        TradeOfferCreate(side="sell", sku=opened.sku, quantity=2, unitPrice="6.25"),
        current_unit_price=opened.unit_price,
    )
    closed = opened.model_copy(
        update={
            "status": "closed",
            "order_id": "b" * 24,
            "counterparty_group": 5,
            "closed_at": datetime.now(UTC),
        }
    )
    return period, dispatch, units, closed


class OwnSaleOrders(TradesStub):
    async def get_order(self, order_id):
        order = await super().get_order(order_id)
        return order.model_copy(update={"supplier_id": "3", "customer_id": "5"})


@pytest.mark.anyio
async def test_sale_closed_while_worker_was_down_is_recovered_once(db_session):
    period, dispatch, units, closed = await published_sale(db_session)
    service = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    result = await service.poll()
    assert result["closed"] == 1
    assert result["custody_pending"] == 1
    trade = db_session.scalar(select(MarketTrade))
    assert trade.role == MarketRole.SELLER
    assert trade.counterparty_id == "5"
    assert trade.closed_at == closed.closed_at
    assert db_session.scalar(select(BillingTask)).external_order_id == closed.order_id
    assert (
        db_session.scalar(
            select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
        )
        is None
    )
    assert all(u.status == "trade_reserved" for u in units[:2])
    # A fresh service simulates a restarted worker, with no in-memory progress.
    again = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    assert (await again.poll())["checked"] == 0
    again.trades.get_offer.assert_not_awaited()
    assert len(db_session.scalars(select(MarketTrade)).all()) == 1
    assert len(db_session.scalars(select(BillingTask)).all()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["open", "closing", "expired", "cancelled"])
async def test_only_confirmed_terminal_states_release_stock(db_session, state):
    period, dispatch, units, closed = await published_sale(db_session)
    observed = closed.model_copy(
        update={"status": state, "order_id": None, "counterparty_group": None}
    )
    service = reconciler(db_session, period, dispatch, observed)
    result = await service.poll()
    released = state in {"expired", "cancelled"}
    assert result["released"] == int(released)
    assert all(
        u.status == ("available" if released else "trade_reserved") for u in units[:2]
    )
    service.trades.get_offer.assert_awaited_once()
    assert db_session.scalar(select(MarketTrade)) is None
    assert db_session.scalar(select(BillingTask)) is None


@pytest.mark.anyio
async def test_429_persists_cooldown_across_worker_restarts(db_session):
    period, dispatch, _, closed = await published_sale(db_session)
    now = datetime.now(UTC)
    service = reconciler(db_session, period, dispatch, closed)
    service.trades.get_offer.side_effect = TradesHTTPError(429, retry_after_seconds=120)
    assert (await service.poll(now=now))["failed"] == 1
    offer = db_session.scalar(select(MarketOffer))
    assert offer.next_retry_at == now + timedelta(seconds=120)
    assert offer.http_status == 429
    restarted = reconciler(db_session, period, dispatch, closed)
    assert (await restarted.poll(now=now + timedelta(seconds=60)))["blocked"] == 1
    restarted.trades.get_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_missing_creation_identity_is_never_guessed_or_republished(db_session):
    period, dispatch, units, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.external_offer_id = None
    offer.status = "creation_uncertain"
    db_session.commit()
    service = reconciler(db_session, period, dispatch, closed)
    assert (await service.poll())["failed"] == 1
    assert "verified association" in offer.last_error
    service.trades.get_offer.assert_not_awaited()
    assert all(u.status == "trade_reserved" for u in units[:2])


@pytest.mark.anyio
async def test_lost_acceptance_closed_by_another_group_releases_our_reservation(
    db_session,
):
    period, dispatch, units, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.publisher_group_id = 5
    offer.side = "buy"
    offer.status = "acceptance_uncertain"
    db_session.commit()
    other_won = closed.model_copy(
        update={"publisher_group": 5, "side": "buy", "counterparty_group": 7}
    )
    service = reconciler(db_session, period, dispatch, other_won)
    result = await service.poll()
    assert result["released"] == 1
    assert all(u.status == "available" for u in units[:2])
    assert db_session.scalar(select(MarketTrade)) is None


@pytest.mark.anyio
async def test_buy_receipt_does_not_wait_for_supplier_invoice(db_session, monkeypatch):
    period, dispatch, _, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.publisher_group_id = 5
    offer.status = "acceptance_uncertain"
    db_session.commit()
    bought = closed.model_copy(update={"publisher_group": 5, "counterparty_group": 3})
    synchronize = AsyncMock()
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )
    service = reconciler(db_session, period, dispatch, bought)
    result = await service.poll()
    assert result["closed"] == 1
    synchronize.assert_awaited_once()
    assert db_session.scalar(select(MarketTrade)).role == MarketRole.BUYER
    assert db_session.scalar(select(BillingTask)) is None


@pytest.mark.anyio
async def test_invoice_enqueue_survives_failure_before_final_reconciliation_commit(
    db_session,
):
    period, dispatch, _, closed = await published_sale(db_session)
    service = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    await service.custody.reconcile_offer(closed)
    # The process dies here, before setting reconciled_at. No duplicate emission.
    recovered = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    assert (await recovered.poll())["closed"] == 1
    assert len(db_session.scalars(select(BillingTask)).all()) == 1


@pytest.mark.anyio
async def test_lost_acceptance_is_confirmed_by_get_without_reaccepting(db_session):
    period, dispatch, _, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.publisher_group_id = 5
    offer.side = "buy"
    offer.status = "acceptance_uncertain"
    db_session.commit()
    ours = closed.model_copy(
        update={"publisher_group": 5, "side": "buy", "counterparty_group": 3}
    )
    service = reconciler(db_session, period, dispatch, ours)
    result = await service.poll()
    assert result["closed"] == 1
    assert db_session.scalar(select(MarketTrade)).role == MarketRole.SELLER
    assert db_session.scalar(select(BillingTask)).external_order_id == closed.order_id
    assert service.inventory.movements == []
    service.trades.accept_offer.assert_not_called()


@pytest.mark.anyio
async def test_own_buy_offer_and_receipt_sync_failure_recover_independently(
    db_session, monkeypatch
):
    period, dispatch, _, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.side = "buy"
    db_session.commit()
    bought = closed.model_copy(update={"side": "buy"})

    class SupplierOrder(TradesStub):
        async def get_order(self, order_id):
            order = await super().get_order(order_id)
            return order.model_copy(update={"supplier_id": "5", "customer_id": "3"})

    synchronize = AsyncMock(side_effect=[TimeoutError(), None])
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )
    service = reconciler(
        db_session, period, dispatch, bought, order_source=SupplierOrder(bought)
    )
    now = datetime.now(UTC)
    assert (await service.poll(now=now))["failed"] == 1
    trade = db_session.scalar(select(MarketTrade))
    assert trade.role == MarketRole.BUYER
    assert offer.reconciled_at is None
    assert (await service.poll(now=now + timedelta(seconds=60)))["closed"] == 1
    assert len(db_session.scalars(select(MarketTrade)).all()) == 1
    assert db_session.scalar(select(BillingTask)) is None
    assert synchronize.await_count == 2
