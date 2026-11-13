from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models import CustodyEvent, MarketRole, Unit
from app.services.inventory.sync import InventorySyncService
from app.services.trades import TradeCustodyService
from tests.support.billing import make_period, make_trade
from tests.support.farma_central import catalog_product
from tests.support.recalls import RecallInventory


@pytest.mark.anyio
async def test_market_receipt_in_buffer_preserves_ids_when_main_store_is_full(
    db_session,
):
    inventory = RecallInventory(db_session)
    lot = inventory.lot("official-market-batch", "RAW")
    existing = inventory.unit("existing-stock", lot)
    period = make_period()
    db_session.add(period)
    db_session.flush()
    trade = make_trade(
        period.id, role=MarketRole.BUYER, sku=lot.product.sku, quantity=1
    )
    db_session.add(trade)
    db_session.commit()
    store, buffer = inventory.locations["storage"], inventory.locations["buffer"]
    client = AsyncMock()
    client.get_available_products.return_value = [
        catalog_product(lot.product.sku, "Raw material")
    ]
    client.get_spaces.return_value = [
        {"_id": store.code, "totalSpace": 1, "usedSpace": 1},
        {"_id": buffer.code, "buffer": True, "totalSpace": 1, "usedSpace": 1},
    ]
    client.get_space_inventory.return_value = [{"sku": lot.product.sku, "quantity": 1}]

    async def products(code, sku, **kwargs):
        return [
            {
                "_id": existing.external_unit_id
                if code == store.code
                else "official-market-unit",
                "sku": sku,
                "store": code,
                "expiresAt": inventory.expiry.isoformat(),
                "batch": lot.external_lot_id,
            }
        ]

    client.get_space_products.side_effect = products
    await InventorySyncService(client, db_session).synchronize()
    TradeCustodyService(
        db_session,
        farma_central=client,
        trades=AsyncMock(),
        purchase_orders=AsyncMock(),
        period=period,
    ).record_trade_units(trade.id, ["official-market-unit"])
    unit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == "official-market-unit")
    )
    assert unit.current_location_id == buffer.id
    assert unit.lot.external_lot_id == lot.external_lot_id
    assert unit.status == "available"
    evidence = db_session.scalar(
        select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
    )
    assert evidence.unit_id == unit.id
    client.move_product.assert_not_awaited()
