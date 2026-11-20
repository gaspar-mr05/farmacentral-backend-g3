from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.models import (
    CustodyEvent,
    Lot,
    LotOrigin,
    MarketOffer,
    MarketRole,
    MarketTrade,
    Order,
    OrderItem,
    OrderSource,
    OrderStatus,
    ProcurementRequest,
    ProductionRun,
    ReplenishmentState,
    Unit,
)
from app.schemas.supply import SupplyResponse
from app.schemas.surplus import SurplusPolicy
from app.services.inventory.sync import EntityChanges, InventorySyncResult
from app.services.trades.surplus import SurplusSalesService
from tests.services.test_procurement_planning import demand, product
from tests.services.test_trade_custody import (
    TradesStub,
    add_market_stock,
    official_offer,
)
from tests.support.farma_central import make_settings
from tests.support.orders import make_external_order


def setup_sales(session, monkeypatch, *, quantity=10, buffer=1):
    period, storage, dispatch, units = add_market_stock(session, quantity=quantity)
    sku = units[0].lot.product.sku
    source = Mock(
        get_available_products=AsyncMock(
            return_value=[product(sku, cost=2).model_dump(by_alias=True)]
        ),
        get_spaces=AsyncMock(
            return_value=[
                {
                    "_id": storage.code,
                    "cold": True,
                    "totalSpace": 100,
                    "usedSpace": quantity,
                },
                {
                    "_id": dispatch.code,
                    "checkOut": True,
                    "cold": True,
                    "totalSpace": 100,
                    "usedSpace": 0,
                },
            ]
        ),
        move_product=AsyncMock(),
    )
    trades = Mock(
        create_offer=AsyncMock(),
        get_offer=AsyncMock(),
        cancel_offer=AsyncMock(),
        accept_offer=AsyncMock(),
        list_offers=AsyncMock(return_value=[]),
    )
    prices = Mock(get_current_prices=AsyncMock(return_value=[{"sku": sku, "price": 5}]))
    policy = SurplusPolicy(skus=(sku,), lot_size=3, raw_buffer_batches=buffer)
    service = SurplusSalesService(
        source, trades, Mock(), prices, session, period, policy=policy
    )

    async def create(request, **kwargs):
        return official_offer(sku=request.sku, quantity=request.quantity).model_copy(
            update={
                "unit_price": request.unit_price,
                "id": f"{int('a' * 24, 16) + trades.create_offer.await_count - 1:024x}",
            }
        )

    trades.create_offer.side_effect = create
    monkeypatch.setattr(
        "app.services.trades.surplus.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock())),
    )
    return service, units, storage, dispatch


def test_only_overdue_production_blocks_market_sales(db_session, monkeypatch):
    service, units, _, _ = setup_sales(db_session, monkeypatch)
    now = datetime.now(UTC)
    run = ProductionRun(
        requested_at=now,
        expected_sku=units[0].lot.product.sku,
        expected_quantity=1,
        available_at=now + timedelta(minutes=1),
    )
    db_session.add(run)
    db_session.commit()

    assert service._production_recovery_pending(now) is False

    run.available_at = now - timedelta(seconds=1)
    db_session.commit()

    assert service._production_recovery_pending(now) is True


@pytest.mark.anyio
async def test_market_stocking_supplies_once_then_publishes_after_receipt(
    db_session, monkeypatch
):
    service, units, storage, _ = setup_sales(
        db_session, monkeypatch, quantity=1, buffer=1
    )
    service.policy = service.policy.model_copy(update={"stock_market": True})
    service.settings = make_settings(replenishment_spending_limit=Decimal("1000"))
    available_at = datetime.now(UTC) + timedelta(minutes=1)
    supply = Mock(
        request=AsyncMock(
            return_value=SupplyResponse(
                sku=units[0].lot.product.sku,
                quantity=3,
                available_at=available_at,
            )
        )
    )
    monkeypatch.setattr(
        "app.services.trades.surplus.SupplyRequestService",
        Mock(return_value=supply),
    )

    first = await service.cycle(execute=True)

    assert first["supplied"] == 1
    assert first["published"] == 0
    supply.request.assert_awaited_once_with(sku=units[0].lot.product.sku, quantity=3)
    state = db_session.scalar(
        select(ReplenishmentState).where(
            ReplenishmentState.sku == units[0].lot.product.sku
        )
    )
    assert state.pending_operation["status"] == "confirmed"

    lot = Lot(
        external_lot_id="market-stocking-receipt",
        product_id=units[0].lot.product_id,
        expires_at=datetime.now(UTC) + timedelta(days=90),
        origin=LotOrigin.FARMA_CENTRAL,
    )
    db_session.add(lot)
    db_session.flush()
    received = [
        Unit(
            external_unit_id=f"market-stocking-unit-{index}",
            lot_id=lot.id,
            current_location_id=storage.id,
            status="available",
            effective_expires_at=datetime.now(UTC) + timedelta(days=90),
        )
        for index in range(3)
    ]
    db_session.add_all(received)
    db_session.commit()

    second = await service.cycle(execute=True)

    assert second["stock_received"] == 1
    assert second["published"] == 1
    assert supply.request.await_count == 1
    db_session.refresh(state)
    assert state.pending_operation is None
    assert state.estimated_spend == 6


@pytest.mark.anyio
async def test_market_stocking_releases_partial_receipt_after_grace(
    db_session, monkeypatch
):
    service, units, storage, _ = setup_sales(
        db_session, monkeypatch, quantity=1, buffer=1
    )
    service.policy = service.policy.model_copy(update={"stock_market": True})
    service.settings = make_settings(replenishment_spending_limit=Decimal("1000"))
    supply = Mock(
        request=AsyncMock(
            return_value=SupplyResponse(
                sku=units[0].lot.product.sku,
                quantity=3,
                available_at=datetime.now(UTC) - timedelta(minutes=3),
            )
        )
    )
    monkeypatch.setattr(
        "app.services.trades.surplus.SupplyRequestService",
        Mock(return_value=supply),
    )
    await service.cycle(execute=True)
    state = db_session.scalar(
        select(ReplenishmentState).where(
            ReplenishmentState.sku == units[0].lot.product.sku
        )
    )
    lot = Lot(
        external_lot_id="partial-market-stocking-receipt",
        product_id=units[0].lot.product_id,
        expires_at=datetime.now(UTC) + timedelta(days=90),
        origin=LotOrigin.FARMA_CENTRAL,
    )
    db_session.add(lot)
    db_session.flush()
    db_session.add_all(
        [
            Unit(
                external_unit_id=f"partial-market-stocking-unit-{index}",
                lot_id=lot.id,
                current_location_id=storage.id,
                status="available",
                effective_expires_at=datetime.now(UTC) + timedelta(days=90),
            )
            for index in range(2)
        ]
    )
    db_session.commit()

    result = {"stock_received": 0}
    service._resume_market_stocking(result)

    assert result["stock_received"] == 1
    db_session.refresh(state)
    assert state.pending_operation is None
    assert state.estimated_spend == 4
    assert state.last_error == "Partial market stocking receipt: 2/3"


@pytest.mark.anyio
async def test_market_stocking_never_exceeds_remaining_spending_room(
    db_session, monkeypatch
):
    service, units, _, _ = setup_sales(db_session, monkeypatch, quantity=1, buffer=1)
    service.policy = service.policy.model_copy(update={"stock_market": True})
    service.settings = make_settings(replenishment_spending_limit=Decimal("1"))
    supply = Mock(request=AsyncMock())
    monkeypatch.setattr(
        "app.services.trades.surplus.SupplyRequestService",
        Mock(return_value=supply),
    )

    result = await service.cycle(execute=True)

    assert result["supplied"] == 0
    assert result["published"] == 0
    supply.request.assert_not_awaited()
    assert db_session.scalar(select(ReplenishmentState)) is None


@pytest.mark.anyio
async def test_market_stocking_reuses_an_idle_replenishment_state(
    db_session, monkeypatch
):
    service, units, _, _ = setup_sales(db_session, monkeypatch, quantity=1, buffer=1)
    service.policy = service.policy.model_copy(update={"stock_market": True})
    service.settings = make_settings(replenishment_spending_limit=Decimal("1000"))
    sku = units[0].lot.product.sku
    state = ReplenishmentState(period_id=service.period.id, sku=sku)
    db_session.add(state)
    db_session.commit()
    supply = Mock(
        request=AsyncMock(
            return_value=SupplyResponse(
                sku=sku,
                quantity=3,
                available_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
    )
    monkeypatch.setattr(
        "app.services.trades.surplus.SupplyRequestService",
        Mock(return_value=supply),
    )

    result = await service.cycle(execute=True)

    assert result["supplied"] == 1
    assert db_session.scalars(select(ReplenishmentState)).all() == [state]
    db_session.refresh(state)
    assert (state.goal or {})["kind"] == "market_stock"


@pytest.mark.anyio
async def test_sales_snapshot_counts_customer_replenishment_once(
    db_session, monkeypatch
):
    service, units, _, _ = setup_sales(db_session, monkeypatch)
    sku = units[0].lot.product.sku
    db_session.add(
        make_external_order(
            status=OrderStatus.ACCEPTED,
            items=[OrderItem(sku=sku, quantity=3, unit_price=5)],
        )
    )
    need = demand(sku=sku, quantity=3, includes_committed=True)
    db_session.add(
        ProcurementRequest(
            period_id=service.period.id,
            request_key="same-customer-order",
            demand=need.model_dump(mode="json"),
            status="waiting",
        )
    )
    db_session.flush()
    result = await service.cycle()
    quote = result["quotes"][0]
    assert quote["protected"] == 4  # Three committed units plus one raw buffer.
    assert quote["quantity"] == 3
    service.trades.create_offer.assert_not_awaited()
    service.trades.accept_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_preview_and_publication_keep_buffer_and_never_duplicate_open_offer(
    db_session, monkeypatch
):
    service, units, storage, _ = setup_sales(db_session, monkeypatch)
    preview = await service.cycle()
    assert preview["quotes"][0]["quantity"] == 3
    service.trades.create_offer.assert_not_awaited()
    result = await service.cycle(execute=True)
    assert result["published"] == 1
    local = db_session.scalar(select(MarketOffer))
    assert local.strategy == "surplus"
    assert local.estimated_unit_cost == 2
    assert local.protected_quantity == 1
    assert all(r.source_location_id == storage.id for r in local.reservations)
    service.trades.get_offer.return_value = (
        await service.trades.create_offer.side_effect(
            Mock(sku=local.sku, quantity=local.quantity, unit_price=local.unit_price)
        )
    )
    again = await service.cycle(execute=True)
    assert again["published"] == 0
    assert service.trades.create_offer.await_count == 1
    assert sum(u.status == "available" for u in units) == 7


@pytest.mark.anyio
async def test_sales_target_counts_pending_offer_before_publishing_another(
    db_session, monkeypatch
):
    service, _, _, _ = setup_sales(db_session, monkeypatch)
    service.policy = service.policy.model_copy(update={"target_sales": 1})
    assert (await service.cycle(execute=True))["published"] == 1
    local = db_session.scalar(select(MarketOffer))
    service.trades.get_offer.return_value = official_offer(
        sku=local.sku, quantity=local.quantity
    ).model_copy(
        update={
            "id": local.external_offer_id,
            "unit_price": local.unit_price,
        }
    )

    result = await service.cycle(execute=True)

    assert result["reason"] == "Sales participation target reached or pending"
    assert result["participation"]["target_per_role"] == 1
    assert service.trades.create_offer.await_count == 1


@pytest.mark.anyio
async def test_known_closed_trade_is_left_to_reconciler_without_extra_scan(
    db_session, monkeypatch
):
    service, _, _, _ = setup_sales(db_session, monkeypatch)
    service.policy = service.policy.model_copy(update={"target_sales": 1})
    await service.cycle(execute=True)
    local = db_session.scalar(select(MarketOffer))
    now = datetime.now(UTC)
    local.status = "closed"
    local.external_order_id = "b" * 24
    local.counterparty_group_id = 5
    db_session.add(
        MarketTrade(
            period_id=service.period.id,
            external_offer_id=local.external_offer_id,
            external_order_id=local.external_order_id,
            role=MarketRole.SELLER,
            counterparty_id="5",
            sku=local.sku,
            quantity=local.quantity,
            unit_price=local.unit_price,
            closed_at=now,
        )
    )
    db_session.commit()
    synchronize = AsyncMock(
        return_value=InventorySyncResult(
            products=EntityChanges(),
            locations=EntityChanges(),
            lots=EntityChanges(),
            units=EntityChanges(),
        )
    )
    monkeypatch.setattr(
        "app.services.trades.surplus.InventorySyncService",
        Mock(return_value=Mock(synchronize=synchronize)),
    )

    result = await service.cycle(execute=True)

    assert result["reason"] == "Sales participation target reached or pending"
    service.trades.get_offer.assert_not_awaited()
    synchronize.assert_awaited_once()


@pytest.mark.anyio
async def test_profitable_open_buy_offer_is_accepted_and_custody_is_recorded(
    db_session, monkeypatch
):
    service, units, _, _ = setup_sales(db_session, monkeypatch)
    sku = units[0].lot.product.sku
    opened = official_offer(sku=sku, side="buy", publisher_group=5, quantity=3)
    closed = opened.model_copy(
        update={
            "status": "closed",
            "counterparty_group": 3,
            "closed_at": datetime.now(UTC),
            "order_id": "b" * 24,
        }
    )
    service.trades.list_offers.return_value = [opened]
    service.trades.accept_offer.return_value = closed
    service.custody._purchase_orders = TradesStub(closed)
    synchronized = InventorySyncResult(
        products=EntityChanges(),
        locations=EntityChanges(),
        lots=EntityChanges(),
        units=EntityChanges(),
        inventory_complete=True,
        missing_unit_ids=tuple(unit.external_unit_id for unit in units[:3]),
    )
    monkeypatch.setattr(
        "app.services.trades.surplus.InventorySyncService",
        Mock(return_value=Mock(synchronize=AsyncMock(return_value=synchronized))),
    )

    result = await service.cycle(execute=True)

    assert result["accepted"] == 1
    assert result["published"] == 0
    service.trades.accept_offer.assert_awaited_once_with(opened.id)
    trade = db_session.scalar(select(MarketTrade))
    assert trade is not None
    assert (
        len(
            db_session.scalars(
                select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
            ).all()
        )
        == 3
    )
    assert all(unit.status == "dispatched" for unit in units[:3])


@pytest.mark.anyio
async def test_customer_demand_reclaims_offer_and_returns_units_to_storage(
    db_session, monkeypatch
):
    service, units, storage, dispatch = setup_sales(db_session, monkeypatch, quantity=5)
    assert (await service.cycle(execute=True))["published"] == 1
    local = db_session.scalar(select(MarketOffer))
    order = Order(
        buyer_name="Customer",
        source=OrderSource.SFTP,
        status=OrderStatus.RECEIVED,
        total=20,
        delivery_deadline=datetime.now(UTC) + timedelta(hours=2),
        items=[OrderItem(sku=local.sku, quantity=5, unit_price=4)],
    )
    db_session.add(order)
    db_session.commit()
    opened = official_offer(sku=local.sku, quantity=3).model_copy(
        update={"unit_price": Decimal(5)}
    )
    service.trades.get_offer.side_effect = [
        opened,
        opened.model_copy(update={"status": "cancelled"}),
    ]
    result = await service.cycle(execute=True)
    assert result["cancelled"] == 1
    assert result["published"] == 0
    assert all(u.current_location_id == storage.id for u in units)
    assert all(u.status == "available" for u in units)
    assert all(r.released_at is not None for r in local.reservations)


@pytest.mark.anyio
async def test_lost_creation_never_republishes_after_restart(db_session, monkeypatch):
    service, units, _, _ = setup_sales(db_session, monkeypatch)
    service.trades.create_offer.side_effect = TimeoutError("lost response")
    assert (await service.cycle(execute=True))["failed"] == 1
    local = db_session.scalar(select(MarketOffer))
    assert local.status == "creation_uncertain"
    assert local.strategy == "surplus"
    local.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    service.trades.create_offer.reset_mock()
    await service.cycle(execute=True)
    service.trades.create_offer.assert_not_awaited()
    assert sum(u.status == "trade_reserved" for u in units) == 3


@pytest.mark.anyio
async def test_atomic_reservation_rejects_stale_pool_without_spending_buffer(
    db_session, monkeypatch
):
    from app.schemas.trades import TradeOfferCreate
    from app.services.trades.custody import (
        InsufficientTradeStockError,
        ReservationGuard,
    )

    service, units, _, _ = setup_sales(db_session, monkeypatch, quantity=4)
    # Snapshot had four free units. One was assigned before reservation.
    units[3].status = "reserved"
    db_session.commit()
    with pytest.raises(InsufficientTradeStockError):
        await service.custody.publish_sell(
            TradeOfferCreate(
                side="sell", sku=units[0].lot.product.sku, quantity=3, unitPrice=5
            ),
            current_unit_price=Decimal(5),
            guard=ReservationGuard([u.id for u in units], 1),
        )
    service.trades.create_offer.assert_not_awaited()
    service.inventory.move_product.assert_not_awaited()


@pytest.mark.anyio
async def test_expired_offer_returns_stock_and_reprices_from_fresh_reference(
    db_session, monkeypatch
):
    service, units, storage, _ = setup_sales(db_session, monkeypatch)
    await service.cycle(execute=True)
    local = db_session.scalar(select(MarketOffer))
    expired = official_offer(sku=local.sku, quantity=3).model_copy(
        update={"unit_price": Decimal(5), "status": "expired"}
    )
    service.trades.get_offer.return_value = expired
    service.prices.get_current_prices.return_value = [{"sku": local.sku, "price": 6}]
    await service.cycle(execute=True)
    calls = service.trades.create_offer.await_args_list
    assert len(calls) == 2
    assert calls[-1].args[0].unit_price == 6
    assert service.prices.get_current_prices.await_args.kwargs["use_cache"] is False


@pytest.mark.anyio
async def test_sale_closure_invoice_and_payment_with_confirmed_units(
    db_session, monkeypatch
):
    from app.models import BillingTask, InvoiceSide, MarketTrade, Unit
    from app.schemas.billing import BillingInvoice
    from app.schemas.purchase_orders import PurchaseOrder
    from app.services.billing.invoices import upsert_invoice
    from app.services.billing.sales import SalesInvoicingService
    from tests.support.billing_soap import invoice_values
    from tests.support.farma_central import make_settings

    service, units, _, _ = setup_sales(db_session, monkeypatch)
    await service.cycle(execute=True)
    local = db_session.scalar(select(MarketOffer))
    now = datetime.now(UTC)
    closed = official_offer(sku=local.sku, quantity=3).model_copy(
        update={
            "status": "closed",
            "unit_price": Decimal(5),
            "order_id": "b" * 24,
            "counterparty_group": 5,
            "closed_at": now,
        }
    )
    order = PurchaseOrder.model_validate(
        {
            "id": "b" * 24,
            "cliente": "5",
            "proveedor": "3",
            "sku": local.sku,
            "cantidad": 3,
            "despachado": 3,
            "precioUnitario": 5,
            "estado": "cumplida",
            "historial": [],
            "creada": local.opened_at,
            "actualizada": now,
            "vencimiento": now + timedelta(hours=1),
            "facturado": False,
        }
    )
    service.trades.get_offer.return_value = closed
    orders = Mock(get_order=AsyncMock(return_value=order))
    service.custody._purchase_orders = orders
    # Protect the remaining stock so this cycle only observes the closure.
    service.policy = service.policy.model_copy(update={"raw_buffer_batches": 100})
    await service.cycle(execute=True)
    trade = db_session.scalar(select(MarketTrade))
    assert db_session.scalar(select(BillingTask)).external_order_id == order.id
    assert service.participation()["sales"] == 1
    assert service.participation()["purchases"] == 0
    assert service.participation()["sales_invoiced"] == 0
    # The simulated market transferred these exact IDs; production never infers them.
    confirmed_ids = [
        db_session.get(Unit, r.unit_id).external_unit_id for r in local.reservations
    ]
    service.custody.record_trade_units(trade.id, confirmed_ids)
    assert sum(u.status == "dispatched" for u in units) == 3
    pending = BillingInvoice.model_validate(
        invoice_values(
            client="5",
            supplier="3",
            channel="mercado",
            price="15",
            interest="0",
            totalPrice="15",
            createdAt=now.isoformat(),
            updatedAt=now.isoformat(),
        )
    )
    billing = Mock(emit_invoice=AsyncMock(return_value=pending))
    seller = SalesInvoicingService(
        billing, orders, db_session, service.period, settings=make_settings()
    )
    assert (await seller.poll())["confirmed"] == 1
    assert service.participation()["sales_invoiced"] == 1
    paid = pending.model_copy(
        update={"status": "paid", "updated_at": now + timedelta(seconds=1)}
    )
    upsert_invoice(
        db_session,
        service.period,
        paid,
        InvoiceSide.SUPPLIER,
        now + timedelta(seconds=1),
    )
    db_session.commit()
    assert service.participation()["sales_paid"] == 1
    await service.cycle(execute=True)
    billing.emit_invoice.assert_awaited_once_with(order.id)


@pytest.mark.anyio
async def test_known_429_keeps_persistent_cooldown_and_does_not_repeat_publication(
    db_session, monkeypatch
):
    from app.clients.trades import TradesHTTPError

    service, units, _, _ = setup_sales(db_session, monkeypatch)
    service.trades.create_offer.side_effect = TradesHTTPError(
        429, retry_after_seconds=120
    )
    result = await service.cycle(execute=True)
    assert result["failed"] == 1
    local = db_session.scalar(select(MarketOffer))
    assert local.status == "failed"
    assert local.http_status == 429
    assert local.next_retry_at > datetime.now(UTC) + timedelta(seconds=110)
    service.trades.create_offer.reset_mock()
    assert (await service.cycle(execute=True))["deferred"] == 1
    service.trades.create_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_known_local_recall_cancels_an_open_offer_even_with_enough_other_stock(
    db_session, monkeypatch
):
    service, units, _, _ = setup_sales(db_session, monkeypatch)
    await service.cycle(execute=True)
    local = db_session.scalar(select(MarketOffer))
    units[0].status = "recall_pending"
    db_session.commit()
    opened = official_offer(sku=local.sku, quantity=3).model_copy(
        update={"unit_price": Decimal(5)}
    )
    service.trades.get_offer.side_effect = [
        opened,
        opened.model_copy(update={"status": "cancelled"}),
    ]
    assert (await service.cycle(execute=True))["cancelled"] == 1
    assert units[0].status == "recall_pending"


@pytest.mark.anyio
async def test_near_expiry_units_are_excluded_from_the_reserved_offer(
    db_session, monkeypatch
):
    service, units, _, _ = setup_sales(db_session, monkeypatch, quantity=4)
    units[0].effective_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    db_session.commit()
    result = await service.cycle(execute=True)
    local = db_session.scalar(select(MarketOffer))
    assert result["published"] == 1
    assert local.quantity == 2
    assert {r.unit_id for r in local.reservations} == {units[1].id, units[2].id}
    assert units[0].status == "available"


@pytest.mark.anyio
async def test_market_cycle_does_not_hold_or_wait_for_packaging_lock(
    db_session, monkeypatch
):
    from sqlalchemy.orm import Session

    from app.services.production.locking import production_lock

    service, units, _, _ = setup_sales(db_session, monkeypatch)

    with Session(db_session.get_bind()) as producer, production_lock(producer):
        result = await service.cycle(execute=True)

    assert result["published"] == 1
    service.trades.create_offer.assert_awaited_once()


@pytest.mark.anyio
async def test_concurrent_market_cycle_is_still_serialized(db_session, monkeypatch):
    from sqlalchemy.orm import Session

    from app.db.operation_locks import operation_lock

    service, _, _, _ = setup_sales(db_session, monkeypatch)
    key = f"surplus-cycle:{service.period.id}"

    with Session(db_session.get_bind()) as other, operation_lock(other, key):
        result = await service.cycle(execute=True)

    assert result["deferred"] == 1
    assert result["reason"] == "Another surplus cycle is already running"
    service.trades.create_offer.assert_not_awaited()


@pytest.mark.anyio
async def test_sellable_kits_remain_protected_for_customer_fulfillment(
    db_session, monkeypatch
):
    service, units, _, _ = setup_sales(db_session, monkeypatch)
    sku = units[0].lot.product.sku
    service.inventory.get_available_products.return_value = [
        product(sku, cost=2)
        .model_copy(update={"sellable": True})
        .model_dump(by_alias=True)
    ]
    result = await service.cycle(execute=True)
    assert result["published"] == 0
    assert result["quotes"][0]["quantity"] == 0
    service.trades.create_offer.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("other_goal", [False, True])
async def test_market_stocking_reuses_idle_state_and_preserves_other_goals(
    db_session, monkeypatch, other_goal
):
    service, units, _, _ = setup_sales(db_session, monkeypatch, quantity=1, buffer=1)
    sku = units[0].lot.product.sku
    service.policy = service.policy.model_copy(update={"stock_market": True})
    service.settings = make_settings(replenishment_spending_limit=Decimal("1000"))
    state = ReplenishmentState(
        period_id=service.period.id,
        sku=sku,
        estimated_spend=Decimal(7),
        goal={"kind": "customer", "quantity": 1} if other_goal else None,
    )
    db_session.add(state)
    db_session.commit()
    identity = state.id
    supply = Mock(
        request=AsyncMock(
            return_value=SupplyResponse(
                sku=sku,
                quantity=3,
                available_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
    )
    monkeypatch.setattr(
        "app.services.trades.surplus.SupplyRequestService", Mock(return_value=supply)
    )
    result = await service.cycle(execute=True)
    rows = db_session.scalars(select(ReplenishmentState)).all()
    assert len(rows) == 1 and rows[0].id == identity
    assert rows[0].estimated_spend == Decimal(7)
    assert result["failed"] == 0
    assert result["supplied"] == (0 if other_goal else 1)
    if other_goal:
        assert rows[0].goal == {"kind": "customer", "quantity": 1}
        supply.request.assert_not_awaited()
    else:
        assert rows[0].pending_operation["status"] == "confirmed"
