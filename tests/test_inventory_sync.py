from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import Location, Lot, Product, ProductCategory, Unit
from app.services.inventory_sync import InventorySyncService


class FakeInventorySource:
    def __init__(self) -> None:
        test_id = uuid4().hex[:8]
        self.sku = f"SYNC-SKU-{test_id}"
        self.store_1 = f"sync-store-1-{test_id}"
        self.store_2 = f"sync-store-2-{test_id}"
        self.lot_id = f"sync-lot-{test_id}"
        self.unit_id = f"sync-unit-{test_id}"
        self.product_name = "Amoxicilina 500 mg"
        self.unit_store: str | None = self.store_1
        self.expires_at = datetime.now(UTC) + timedelta(days=30)

    async def get_available_products(self):
        return [
            {
                "sku": self.sku,
                "name": self.product_name,
                "production": {"batch": 50, "at": "farma-central"},
                "sellable": False,
                "storage": {"cold": False},
            }
        ]

    async def get_spaces(self):
        return [
            {"_id": self.store_1, "checkIn": True, "cold": False},
            {"_id": self.store_2, "cold": True},
        ]

    async def get_space_inventory(self, store_id: str):
        quantity = int(store_id == self.unit_store)
        return [{"sku": self.sku, "quantity": quantity}]

    async def get_space_products(self, store_id: str, sku: str):
        if store_id != self.unit_store:
            return []
        return [
            {
                "_id": self.unit_id,
                "sku": sku,
                "store": store_id,
                "expiresAt": self.expires_at.isoformat(),
                "batch": self.lot_id,
            }
        ]


@pytest.mark.anyio
async def test_sync_is_idempotent_and_updates_external_changes(db_session) -> None:
    client = FakeInventorySource()
    service = InventorySyncService(client, db_session)

    first = await service.synchronize()
    second = await service.synchronize()

    assert first.products.created == 1
    assert first.locations.created == 2
    assert first.lots.created == 1
    assert first.units.created == 1
    assert second.products.created == second.products.updated == 0
    assert second.locations.created == second.locations.updated == 0
    assert second.lots.created == second.lots.updated == 0
    assert second.units.created == second.units.updated == 0
    assert _count(db_session, Product, Product.sku == client.sku) == 1
    assert (
        _count(
            db_session,
            Location,
            Location.code.in_((client.store_1, client.store_2)),
        )
        == 2
    )
    assert _count(db_session, Lot, Lot.external_lot_id == client.lot_id) == 1
    assert _count(db_session, Unit, Unit.external_unit_id == client.unit_id) == 1

    client.product_name = "Amoxicilina actualizada"
    client.unit_store = client.store_2
    client.expires_at += timedelta(days=1)
    changed = await service.synchronize()

    product = db_session.scalar(select(Product).where(Product.sku == client.sku))
    lot = db_session.scalar(select(Lot).where(Lot.external_lot_id == client.lot_id))
    unit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == client.unit_id)
    )

    assert changed.products.updated == 1
    assert changed.lots.updated == 1
    assert changed.units.updated == 1
    assert product is not None
    assert product.name == "Amoxicilina actualizada"
    assert product.category is ProductCategory.INSUMO
    assert lot is not None
    assert lot.expires_at == client.expires_at
    assert unit is not None
    assert unit.current_location.code == client.store_2

    client.unit_store = None
    missing = await service.synchronize()

    db_session.refresh(unit)
    assert missing.units.updated == 1
    assert unit.status == "unavailable"


def _count(db_session, model, criterion) -> int:
    return db_session.scalar(select(func.count()).select_from(model).where(criterion))
