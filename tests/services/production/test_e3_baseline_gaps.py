"""Reproduce known E2 limitations for the E3 gap register; no external writes."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.clients.purchase_orders import PurchaseOrdersHTTPError
from app.models import OrderStatus, Unit
from app.services.inventory.collection import InventorySnapshotChangedError
from app.services.orders import ExternalOrderFulfillmentService
from app.services.production.kits import KitProductionService
from tests.services.inventory.test_sync import FakeInventorySource
from tests.services.test_external_order_regressions import prepare_dispatch
from tests.support.farma_central import catalog_product, make_settings


@pytest.mark.anyio
async def test_unrelated_inventory_movements_exhaust_replenishment_snapshot_retries(
    db_session, monkeypatch, caplog
):
    class MovingInventory(FakeInventorySource):
        reads = 0

        async def get_available_products(self):
            return [
                *(await super().get_available_products()),
                catalog_product("KIT-DERMATO", "Dermato", sellable=True),
            ]

        async def get_space_products(self, *args, **kwargs):
            self.reads += 1
            return []  # A different SKU left after its inventory count was read.

    client = MovingInventory()
    client.sku = "KIT-RESP-ADULTO"
    client.request_products = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.production.kits.asyncio.sleep", sleep)
    service = KitProductionService(client, db_session, raw_material_source="supply")
    before = db_session.scalar(select(func.count()).select_from(Unit))

    with pytest.raises(InventorySnapshotChangedError, match="KIT-RESP-ADULTO"):
        await service.maintain_kit_stock(
            minimum=4, target=8, minimum_lifetime_seconds=0
        )

    assert client.reads == 3
    assert "attempt=3/3" in caplog.text
    assert db_session.scalar(select(func.count()).select_from(Unit)) == before
    client.request_products.assert_not_awaited()


@pytest.mark.anyio
async def test_oc_429_keeps_order_but_current_worker_retries_before_retry_after(
    db_session,
):
    order, _, farma, _ = prepare_dispatch(db_session)
    orders = AsyncMock()
    orders.get_order.side_effect = PurchaseOrdersHTTPError(429, retry_after_seconds=120)
    service = ExternalOrderFulfillmentService(
        farma, orders, db_session, settings=make_settings()
    )

    first = await service.process_pending()
    second = await service.process_pending()

    assert first.failed == second.failed == 1
    assert orders.get_order.await_count == 2  # No durable 120-second delay today.
    assert order.status == OrderStatus.ACCEPTED
    assert farma.dispatches == []
