from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.clients.trades import TradesHTTPError
from app.models import (
    MarketOffer,
    MarketRole,
    MarketTrade,
    ProcurementRequest,
    ReplenishmentState,
)
from app.queries.market import accounted_deliveries
from app.schemas.procurement import ProcurementAction, ProcurementPlan
from app.services.inventory.sync import EntityChanges, InventorySyncResult
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


def completed_sync(units):
    return InventorySyncResult(
        products=EntityChanges(),
        locations=EntityChanges(),
        lots=EntityChanges(),
        units=EntityChanges(),
        inventory_complete=True,
        created_unit_ids=tuple(unit.external_unit_id for unit in units),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("proof", ["delivered", "no_trade", "not_accounted", "open"])
async def test_historical_delivered_purchase_does_not_block_new_supply(
    db_session, monkeypatch, proof
):
    period, storage, _, units = add_market_stock(db_session, quantity=3)
    units[0].lot.product.sku = RAW
    old = MarketOffer(
        period_id=period.id,
        external_offer_id="d" * 24,
        external_order_id="e" * 24,
        side="buy",
        publisher_group_id=3,
        sku=RAW,
        quantity=2,
        unit_price=2,
        status="open" if proof == "open" else "closed",
    )
    db_session.add(old)
    db_session.flush()
    db_session.add(
        ProcurementRequest(
            period_id=period.id,
            request_key="historical-purchase",
            market_offer_id=old.id,
            demand={},
            status="custody_pending",
            decision={"replenishment_accounted": proof != "not_accounted"},
        )
    )
    if proof != "no_trade":
        db_session.add(
            MarketTrade(
                period_id=period.id,
                external_offer_id=old.external_offer_id,
                external_order_id=old.external_order_id,
                role=MarketRole.BUYER,
                counterparty_id="5",
                sku=RAW,
                quantity=2,
                unit_price=2,
                closed_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
    db_session.commit()
    source = ScarcityProcurementService(
        Mock(
            get_available_products=AsyncMock(
                return_value=[product(RAW).model_dump(by_alias=True)]
            ),
            get_spaces=AsyncMock(
                return_value=[
                    {
                        "_id": storage.code,
                        "totalSpace": 100,
                        "usedSpace": 3,
                        "cold": True,
                    }
                ]
            ),
        ),
        Mock(list_offers=AsyncMock(return_value=[offer(RAW, 2, 2)])),
        Mock(),
        Mock(get_current_prices=AsyncMock(return_value=[])),
        db_session,
        period,
        tolerant_inventory=True,
    )
    monkeypatch.setattr(
        "app.services.supply.procurement.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock())),
    )

    quote = await source.preview(demand(sku=RAW, quantity=5))

    assert list(db_session.scalars(accounted_deliveries(period.id))) == (
        [old.id] if proof == "delivered" else []
    )
    if proof in {"delivered", "open"}:
        assert [(a.kind, a.quantity) for a in quote.actions] == [("accept", 2)]
    else:
        assert quote.status == "waiting" and quote.actions == []
    assert old.reconciled_at is None
    if proof == "delivered":
        source.preview = AsyncMock(return_value=quote)
        source.trades.get_offer = AsyncMock(return_value=offer(RAW, 2, 2))
        source.custody.accept_sell = AsyncMock()
        current = await source.ensure(
            "new-supply-after-delivery", demand(sku=RAW, quantity=5)
        )
        source.custody.accept_sell.assert_awaited_once()
        assert current.market_offer_id != old.id
        assert old.status == "closed" and old.reconciled_at is None


@pytest.mark.anyio
async def test_unexpected_sourcing_error_logs_location_without_private_data(
    db_session, caplog
):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    source.preview.side_effect = KeyError("PRIVATE_SECRET_IN_EXCEPTION")

    request = await source.ensure("unexpected-error", demand())

    assert request.status == "waiting"
    assert request.last_error == "KeyError"
    assert "error_type=KeyError location=" in caplog.text
    assert "PRIVATE_SECRET_IN_EXCEPTION" not in caplog.text
    source.trades.create_offer.assert_not_awaited()


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
async def test_existing_bid_reuses_cycle_quotes_only_for_observation(db_session):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    opened = offer(RAW, 4, 2, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    need = demand(sku=RAW, quantity=4)
    snapshot, cache = ({}, [], {}), {}
    row = await source.ensure(
        "reuse-quotes", need, snapshot=snapshot, offer_cache=cache
    )
    # Creating a new acquisition still replans without the cycle snapshot.
    assert source.preview.await_args_list[0].kwargs == {}
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    source.preview.reset_mock()
    await source.ensure("reuse-quotes", need, snapshot=snapshot, offer_cache=cache)
    source.preview.assert_awaited_once_with(
        need,
        replacing_offer_id=row.market_offer_id,
        snapshot=snapshot,
        offer_cache=cache,
    )
    source.trades.create_offer.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("scan_after_close", [True, False])
@pytest.mark.parametrize("after_cancellation", [True, False])
async def test_receipt_observation_must_start_after_official_closure(
    db_session, monkeypatch, scan_after_close, after_cancellation
):
    from tests.services.test_trade_custody import TradesStub

    period, _, _, units = add_market_stock(db_session, quantity=4)
    units[0].lot.product.sku = RAW
    db_session.commit()
    opened = offer(RAW, 4, 2)
    if after_cancellation:
        opened = opened.model_copy(update={"side": "buy", "publisher_group": 3})
    closed = opened.model_copy(
        update={
            "status": "closed",
            "counterparty_group": 5 if after_cancellation else 3,
            "order_id": "b" * 24,
            "closed_at": datetime.now(UTC),
        }
    )
    source = service(db_session, period)
    source.trades.get_offer.return_value = closed
    if after_cancellation:
        source.trades.get_offer.side_effect = [opened, closed]
        source.preview.return_value = ProcurementPlan(status="ready")
    source.custody._purchase_orders = TradesStub(closed)
    if after_cancellation:
        order = await source.custody._purchase_orders.get_order(closed.order_id)
        source.custody._purchase_orders = Mock(
            get_order=AsyncMock(
                return_value=order.model_copy(update={"supplier_id": "5"})
            )
        )
    local = source.custody._store_offer(opened, status="open")
    need = demand(sku=RAW, quantity=4)
    row = ProcurementRequest(
        period_id=period.id,
        request_key="existing-receipt",
        demand=need.model_dump(mode="json"),
        status="waiting",
        market_offer_id=local.id,
    )
    db_session.add(row)
    db_session.commit()
    sync = completed_sync(units)
    synchronize = AsyncMock(return_value=sync)
    monkeypatch.setattr(
        "app.services.supply.procurement.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )
    observed_at = closed.closed_at + timedelta(seconds=1 if scan_after_close else -1)
    row = await source.ensure(
        row.request_key, need, inventory_observation=(observed_at, sync)
    )
    assert row.status == "acquired"
    assert synchronize.await_count == (
        0 if scan_after_close and not after_cancellation else 1
    )
    source.trades.accept_offer.assert_not_awaited()
    source.trades.create_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_retarget_retires_bid_only_after_confirmed_cancellation(db_session):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    opened = offer(RAW, 4, 2, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    need = demand(sku=RAW, quantity=4)
    row = await source.ensure("retarget", need)
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    source.trades.get_offer.side_effect = [
        opened,
        opened.model_copy(update={"status": "cancelled"}),
    ]
    source.preview.reset_mock()
    result = await source.ensure("retarget", need, retire=True)
    assert result.status == "superseded" and result.market_offer_id is None
    source.trades.cancel_offer.assert_awaited_once_with(opened.id)
    source.preview.assert_not_awaited()
    source.trades.create_offer.assert_awaited_once()
    source.trades.accept_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_unrelated_purchase_does_not_cancel_a_still_needed_input_bid(db_session):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    opened = offer(RAW, 4, 2, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    need = demand(includes_committed=True, priority=1)
    row = await source.ensure("keep-needed-input", need)
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    source.preview.return_value = ProcurementPlan(
        status="waiting",
        actions=[
            ProcurementAction(
                kind="bid", sku=RAW, quantity=4, useful_quantity=4, cost=8
            ),
            ProcurementAction(
                kind="accept",
                sku="EXC-SUERO-FISIO",
                quantity=2,
                useful_quantity=2,
                cost=2,
                offer_id="d" * 24,
            ),
        ],
    )
    await source.ensure("keep-needed-input", need)
    source.trades.cancel_offer.assert_not_awaited()
    source.trades.create_offer.assert_awaited_once()
    source.trades.accept_offer.assert_not_awaited()
    assert row.market_offer_id is not None
    assert row.status == "waiting"


@pytest.mark.anyio
async def test_customer_kit_bid_yields_to_feasible_waiting_recipe(db_session):
    period, _, _, _ = add_market_stock(db_session)
    need = demand(includes_committed=True, priority=1)
    root_bid = ProcurementPlan(
        status="waiting",
        actions=[
            ProcurementAction(
                kind="bid", sku=need.sku, quantity=2, useful_quantity=2, cost=8
            )
        ],
    )
    source = service(db_session, period, result=root_bid)
    opened = offer(need.sku, 2, 4, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    row = await source.ensure("recipe-handoff", need)
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    source.preview.return_value = ProcurementPlan(
        status="waiting",
        actions=[
            ProcurementAction(
                kind="bid", sku=RAW, quantity=4, useful_quantity=4, cost=8
            )
        ],
    )
    source.trades.get_offer.side_effect = [
        opened,
        opened.model_copy(update={"status": "cancelled"}),
    ]

    row = await source.ensure("recipe-handoff", need)

    source.trades.cancel_offer.assert_awaited_once_with(opened.id)
    assert row.market_offer_id is None
    source.trades.accept_offer.assert_not_awaited()
    assert row.decision["latest_preview"]["actions"][0]["sku"] == RAW


@pytest.mark.anyio
@pytest.mark.parametrize("accept_first", [False, True])
async def test_customer_bid_yields_to_available_lots_of_same_scarce_input(
    db_session, accept_first
):
    period, _, _, _ = add_market_stock(db_session)
    source = service(db_session, period)
    need = demand(sku=RAW, quantity=4, includes_committed=True, priority=1)
    opened = offer(RAW, 4, 2, publisher=3).model_copy(update={"side": "buy"})
    source.trades.create_offer.return_value = opened
    source.trades.get_offer.return_value = opened
    row = await source.ensure("partial-input-handoff", need)
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    source.preview.return_value = ProcurementPlan(
        status="waiting",
        cost=8,
        consumed_cost=8,
        seconds=3660,
        actions=[
            ProcurementAction(
                kind="accept",
                sku=RAW,
                quantity=2,
                useful_quantity=2,
                cost=4,
                offer_id="b" * 24,
            ),
            ProcurementAction(
                kind="bid",
                sku=RAW,
                quantity=2,
                useful_quantity=2,
                cost=4,
            ),
        ],
    )
    if not accept_first:
        source.preview.return_value.actions.reverse()
    source.trades.get_offer.side_effect = [
        opened,
        opened.model_copy(update={"status": "cancelled"}),
    ]

    row = await source.ensure("partial-input-handoff", need)

    if not accept_first:
        source.trades.cancel_offer.assert_not_awaited()
        assert row.market_offer_id is not None
        return
    source.trades.cancel_offer.assert_awaited_once_with(opened.id)
    assert row.market_offer_id is None
    assert row.status == "waiting"
    source.trades.accept_offer.assert_not_awaited()
    assert row.decision["latest_preview"]["actions"][-1]["quantity"] == 2


@pytest.mark.anyio
async def test_selected_recipe_input_survives_a_retry(db_session):
    from tests.services.test_procurement_planning import OTHER

    period, _, _, _ = add_market_stock(db_session)
    need = demand()
    row = ProcurementRequest(
        period_id=period.id,
        request_key="selected-input",
        demand=need.model_dump(mode="json"),
        decision={"next_action_sku": OTHER},
        status="waiting",
    )
    db_session.add(row)
    db_session.commit()
    source = service(db_session, period)
    source.preview.side_effect = TradesHTTPError(429)
    await source.ensure(row.request_key, need)
    row.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    source.preview.side_effect = None
    source.preview.return_value = ProcurementPlan(
        status="waiting",
        actions=[
            ProcurementAction(
                kind="bid", sku=sku, quantity=4, useful_quantity=4, cost=8
            )
            for sku in (RAW, OTHER)
        ],
    )
    source.trades.create_offer.return_value = offer(
        OTHER, 4, 2, publisher=3
    ).model_copy(update={"side": "buy"})
    source.trades.get_offer.return_value = source.trades.create_offer.return_value

    await source.ensure(row.request_key, need)

    assert source.trades.create_offer.call_args.args[0].sku == OTHER
    assert row.decision["next_action_sku"] == OTHER


@pytest.mark.anyio
async def test_input_already_satisfied_returns_to_recipe_instead_of_buying_another(
    db_session,
):
    period, _, _, _ = add_market_stock(db_session)
    need = demand()
    row = ProcurementRequest(
        period_id=period.id,
        request_key="satisfied-input",
        demand=need.model_dump(mode="json"),
        decision={"next_action_sku": RAW},
        status="waiting",
    )
    db_session.add(row)
    db_session.commit()
    source = service(db_session, period, result=ProcurementPlan(status="ready"))

    await source.ensure(row.request_key, need)

    assert row.status == "superseded"
    source.trades.create_offer.assert_not_awaited()
    source.trades.accept_offer.assert_not_awaited()


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
@pytest.mark.parametrize("full_book", [False, True])
async def test_recipe_quotes_use_one_book_without_losing_capped_offers(
    db_session, full_book
):
    from app.schemas.farma_central import FarmaCentralSpace
    from tests.services.test_procurement_planning import KIT, catalog
    from tests.support.billing import make_period

    period = make_period()
    db_session.add(period)
    db_session.commit()
    useful = offer(RAW, 4, 2)
    book = [offer("UNRELATED", 1, 1)] * 200 if full_book else [useful]

    async def listed(**filters):
        if "sku" not in filters:
            return book
        return [useful] if filters["sku"] == RAW else []

    trades = Mock(list_offers=AsyncMock(side_effect=listed))
    source = ScarcityProcurementService(
        Mock(), trades, Mock(), Mock(), db_session, period
    )
    snapshot = (
        catalog(),
        [
            FarmaCentralSpace.model_validate(
                {"_id": "STORE", "totalSpace": 100, "usedSpace": 0}
            )
        ],
        {},
    )
    cache = {}
    first = await source.preview(demand(), snapshot=snapshot, offer_cache=cache)
    assert any(a.offer_id == useful.id for a in first.actions)
    calls = trades.list_offers.await_count
    assert calls == (1 + len(catalog()) if full_book else 1)
    trades.list_offers.assert_any_await(side="sell", state="open")
    # The same decision can test a later order against these quotes. A new
    # decision must read again; execution still performs its normal preflight.
    await source.preview(demand(sku=KIT), snapshot=snapshot, offer_cache=cache)
    assert trades.list_offers.await_count == calls
    await source.preview(demand(), snapshot=snapshot)
    assert trades.list_offers.await_count == calls * 2


@pytest.mark.anyio
async def test_purchase_is_confirmed_once_with_cost_and_supplier_trace(
    db_session, monkeypatch
):
    from app.models import MarketTrade
    from tests.services.test_trade_custody import TradesStub

    period, _, _, units = add_market_stock(db_session, quantity=4)
    units[0].lot.product.sku = RAW
    db_session.commit()
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
    synchronize = AsyncMock(return_value=completed_sync(units))
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
@pytest.mark.parametrize("parts", [1, 2])
async def test_better_sell_offer_cancels_existing_bid_before_replanning(
    db_session, parts
):
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
                quantity=4 // parts,
                useful_quantity=4 // parts,
                cost=4 // parts,
                offer_id=letter * 24,
            )
            for letter in "de"[:parts]
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
async def test_market_stocking_goal_is_not_a_customer_commitment(
    db_session, monkeypatch
):
    period, storage, _, units = add_market_stock(db_session, quantity=3)
    sku = units[0].lot.product.sku
    db_session.add(
        ReplenishmentState(
            period_id=period.id,
            sku="EXC-LACTOSA-DC",
            goal={"kind": "market_stock", "quantity": 20},
        )
    )
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
        tolerant_inventory=True,
    )
    monkeypatch.setattr(
        "app.services.supply.procurement.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock())),
    )

    quote = await source.preview(
        demand(sku=sku, quantity=3, includes_committed=True, priority=1)
    )

    assert quote.status == "ready"


@pytest.mark.anyio
@pytest.mark.parametrize("retire", [False, True])
async def test_cancel_race_recognizes_purchase_instead_of_accepting_another_offer(
    db_session, monkeypatch, retire
):
    from tests.services.test_trade_custody import TradesStub

    period, _, _, units = add_market_stock(db_session, quantity=4)
    units[0].lot.product.sku = RAW
    db_session.commit()
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
        Mock(
            return_value=Mock(synchronize=AsyncMock(return_value=completed_sync(units)))
        ),
    )
    await source.ensure("cancel-race", need, retire=retire)
    assert row.status == "acquired"
    assert row.decision["acquisition"]["order_id"] == "e" * 24
    source.trades.accept_offer.assert_not_awaited()
