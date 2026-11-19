from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.clients.trades import TradesHTTPError
from app.models import (
    BillingTask,
    CustodyEvent,
    CustodyEventType,
    MarketOffer,
    MarketRole,
    MarketTrade,
)
from app.schemas.trades import TradeOfferCreate
from app.services.inventory.sync import EntityChanges, InventorySyncResult
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


def inventory_sync(*, created=(), missing=()):
    return InventorySyncResult(
        products=EntityChanges(),
        locations=EntityChanges(),
        lots=EntityChanges(),
        units=EntityChanges(),
        inventory_complete=True,
        created_unit_ids=tuple(created),
        missing_unit_ids=tuple(missing),
    )


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
async def test_sale_closed_while_worker_was_down_is_recovered_once(
    db_session, monkeypatch
):
    period, dispatch, units, closed = await published_sale(db_session)
    service = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    synchronize = AsyncMock(
        return_value=inventory_sync(
            missing=[unit.external_unit_id for unit in units[:2]]
        )
    )
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )
    result = await service.poll()
    assert result["closed"] == 1
    assert result["custody_pending"] == 0
    trade = db_session.scalar(select(MarketTrade))
    assert trade.role == MarketRole.SELLER
    assert trade.counterparty_id == "5"
    assert trade.closed_at == closed.closed_at
    assert db_session.scalar(select(BillingTask)).external_order_id == closed.order_id
    assert (
        len(
            db_session.scalars(
                select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
            ).all()
        )
        == 2
    )
    assert all(u.status == "dispatched" for u in units[:2])
    # A fresh service simulates a restarted worker, with no in-memory progress.
    again = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    assert (await again.poll())["checked"] == 0
    again.trades.get_offer.assert_not_awaited()
    assert len(db_session.scalars(select(MarketTrade)).all()) == 1
    assert len(db_session.scalars(select(BillingTask)).all()) == 1


@pytest.mark.anyio
async def test_closed_trade_with_ambiguous_unit_delta_remains_retryable(
    db_session, monkeypatch
):
    period, dispatch, units, closed = await published_sale(db_session)
    service = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    synchronize = AsyncMock(
        return_value=inventory_sync(missing=[units[0].external_unit_id])
    )
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )
    now = datetime.now(UTC)

    first = await service.poll(now=now)
    second = await service.poll(now=now + timedelta(seconds=60))

    assert first["closed"] == second["closed"] == 0
    assert first["pending"] == second["pending"] == 1
    assert first["custody_pending"] == second["custody_pending"] == 1
    assert db_session.scalar(select(MarketOffer)).reconciled_at is None
    assert synchronize.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("same_sku", [False, True])
async def test_one_inventory_observation_is_shared_safely_per_poll(
    db_session, monkeypatch, same_sku
):
    period, dispatch, units, closed = await published_sale(db_session)
    first_offer = db_session.scalar(select(MarketOffer))
    first_trade = MarketTrade(
        period_id=period.id,
        external_offer_id=first_offer.external_offer_id,
        external_order_id=closed.order_id,
        role=MarketRole.BUYER,
        counterparty_id="5",
        sku=closed.sku,
        quantity=2,
        unit_price=closed.unit_price,
        closed_at=closed.closed_at,
    )
    second_offer = MarketOffer(
        period_id=period.id,
        external_offer_id="c" * 24,
        external_order_id="d" * 24,
        side=first_offer.side,
        publisher_group_id=3,
        counterparty_group_id=5,
        sku=closed.sku if same_sku else "OTHER-SKU",
        quantity=1,
        unit_price=closed.unit_price,
        status="closed",
    )
    second_trade = MarketTrade(
        period_id=period.id,
        external_offer_id=second_offer.external_offer_id,
        external_order_id=second_offer.external_order_id,
        role=MarketRole.BUYER,
        counterparty_id="5",
        sku=second_offer.sku,
        quantity=1,
        unit_price=closed.unit_price,
        closed_at=closed.closed_at,
    )
    first_offer.status = "closed"
    db_session.add_all([first_trade, second_offer, second_trade])
    db_session.commit()
    service = reconciler(db_session, period, dispatch, closed)
    service.trades.get_offer.side_effect = [
        SimpleNamespace(status="closed"),
        SimpleNamespace(status="closed"),
    ]
    service.custody.reconcile_offer = AsyncMock(side_effect=[first_trade, second_trade])
    service.custody.record_synchronized_trade_units = Mock(return_value=False)
    synchronize = AsyncMock(return_value=inventory_sync())
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )

    result = await service.poll()

    assert result["checked"] == 2
    assert result["custody_pending"] == 2
    synchronize.assert_awaited_once()
    observations = [
        call.args[1]
        for call in service.custody.record_synchronized_trade_units.call_args_list
    ]
    assert observations[0] is not None
    if same_sku:
        assert observations[1] is None
    else:
        assert observations[1] is observations[0]


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
@pytest.mark.parametrize("sku_complete", [True, False])
@pytest.mark.parametrize("extra_receipt", [True, False])
async def test_buy_recovers_retained_receipts_when_only_its_sku_is_complete(
    db_session, monkeypatch, sku_complete, extra_receipt
):
    period, dispatch, units, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.publisher_group_id = 5
    offer.status = "acceptance_uncertain"
    bought = closed.model_copy(update={"publisher_group": 5, "counterparty_group": 3})
    for unit in units[: 3 if extra_receipt else 2]:
        db_session.add(
            CustodyEvent(
                unit_id=unit.id,
                event_type=CustodyEventType.RECEIVED,
                to_location_id=unit.current_location_id,
                occurred_at=bought.closed_at + timedelta(seconds=2),
            )
        )
    db_session.commit()
    snapshot = InventorySyncResult(
        products=EntityChanges(),
        locations=EntityChanges(),
        lots=EntityChanges(),
        units=EntityChanges(),
        inventory_complete=False,
        complete_skus=frozenset({bought.sku} if sku_complete else {"OTHER-SKU"}),
    )
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock(return_value=snapshot))),
    )
    service = reconciler(db_session, period, dispatch, bought)
    result = await service.poll()
    can_link = sku_complete and not extra_receipt
    assert result["closed"] == int(can_link)
    assert result["custody_pending"] == int(not can_link)
    trade = db_session.scalar(select(MarketTrade))
    recorded = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
    ).all()
    assert len(recorded) == (2 if can_link else 0)


@pytest.mark.anyio
async def test_partial_global_inventory_does_not_confirm_seller_custody(
    db_session, monkeypatch
):
    period, dispatch, units, closed = await published_sale(db_session)
    snapshot = InventorySyncResult(
        products=EntityChanges(),
        locations=EntityChanges(),
        lots=EntityChanges(),
        units=EntityChanges(),
        inventory_complete=False,
        complete_skus=frozenset({closed.sku}),
        missing_unit_ids=tuple(u.external_unit_id for u in units[:2]),
    )
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock(return_value=snapshot))),
    )
    result = await reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    ).poll()
    assert result["closed"] == 0
    assert result["custody_pending"] == 1


@pytest.mark.anyio
async def test_buy_receipt_does_not_wait_for_supplier_invoice(db_session, monkeypatch):
    period, dispatch, units, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.publisher_group_id = 5
    offer.status = "acceptance_uncertain"
    db_session.commit()
    bought = closed.model_copy(update={"publisher_group": 5, "counterparty_group": 3})
    synchronize = AsyncMock(
        return_value=inventory_sync(
            created=[unit.external_unit_id for unit in units[:2]]
        )
    )
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
    db_session, monkeypatch
):
    period, dispatch, units, closed = await published_sale(db_session)
    service = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    await service.custody.reconcile_offer(closed)
    # The process dies here, before setting reconciled_at. No duplicate emission.
    recovered = reconciler(
        db_session, period, dispatch, closed, order_source=OwnSaleOrders(closed)
    )
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(
            return_value=Mock(
                synchronize=AsyncMock(
                    return_value=inventory_sync(
                        missing=[unit.external_unit_id for unit in units[:2]]
                    )
                )
            )
        ),
    )
    assert (await recovered.poll())["closed"] == 1
    assert len(db_session.scalars(select(BillingTask)).all()) == 1


@pytest.mark.anyio
async def test_lost_acceptance_is_confirmed_by_get_without_reaccepting(
    db_session, monkeypatch
):
    period, dispatch, units, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.publisher_group_id = 5
    offer.side = "buy"
    offer.status = "acceptance_uncertain"
    db_session.commit()
    ours = closed.model_copy(
        update={"publisher_group": 5, "side": "buy", "counterparty_group": 3}
    )
    service = reconciler(db_session, period, dispatch, ours)
    monkeypatch.setattr(
        "app.services.trades.reconciliation.InventorySyncService",
        Mock(
            return_value=Mock(
                synchronize=AsyncMock(
                    return_value=inventory_sync(
                        missing=[unit.external_unit_id for unit in units[:2]]
                    )
                )
            )
        ),
    )
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
    period, dispatch, units, closed = await published_sale(db_session)
    offer = db_session.scalar(select(MarketOffer))
    offer.side = "buy"
    db_session.commit()
    bought = closed.model_copy(update={"side": "buy"})

    class SupplierOrder(TradesStub):
        async def get_order(self, order_id):
            order = await super().get_order(order_id)
            return order.model_copy(update={"supplier_id": "5", "customer_id": "3"})

    synchronize = AsyncMock(
        side_effect=[
            TimeoutError(),
            inventory_sync(created=[unit.external_unit_id for unit in units[:2]]),
        ]
    )
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
