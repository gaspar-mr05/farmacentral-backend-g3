import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.units import upsert_units
from app.models import (
    CustodyEvent,
    Location,
    Lot,
    LotOrigin,
    Product,
    ProductCategory,
    Unit,
)
from app.schemas.units import UnitData
from app.services.inventory.sync import InventorySyncService


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
        self.include_batch = True
        self.requested_limits: list[int | None] = []

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

    async def get_space_products(
        self,
        store_id: str,
        sku: str,
        *,
        limit: int | None = None,
    ):
        self.requested_limits.append(limit)
        if store_id != self.unit_store:
            return []
        unit = {
            "_id": self.unit_id,
            "sku": sku,
            "store": store_id,
            "expiresAt": self.expires_at.isoformat(),
        }
        if self.include_batch:
            unit["batch"] = self.lot_id
        return [unit]


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
    assert unit.effective_expires_at == client.expires_at

    client.unit_store = None
    missing = await service.synchronize()

    db_session.refresh(unit)
    assert missing.units.updated == 1
    assert unit.status == "unavailable"


@pytest.mark.anyio
async def test_sync_accepts_missing_external_batch(db_session) -> None:
    client = FakeInventorySource()
    client.include_batch = False

    result = await InventorySyncService(client, db_session).synchronize()

    fallback_lot_id = f"unreported:{client.unit_id}"
    assert result.lots.created == 1
    assert result.units.created == 1
    assert _count(db_session, Lot, Lot.external_lot_id == fallback_lot_id) == 1
    assert client.requested_limits == [1]


def test_concurrent_syncs_do_not_insert_duplicate_unreported_lots(test_engine) -> None:
    barrier = Barrier(2)
    client = FakeInventorySource()
    client.include_batch = False
    original_get_space_products = client.get_space_products

    async def synchronized_get_space_products(*args, **kwargs):
        units = await original_get_space_products(*args, **kwargs)
        barrier.wait(timeout=5)
        return units

    client.get_space_products = synchronized_get_space_products

    def synchronize() -> None:
        with Session(test_engine) as session:
            asyncio.run(InventorySyncService(client, session).synchronize())

    fallback_lot_id = f"unreported:{client.unit_id}"
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(synchronize) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)

        with Session(test_engine) as session:
            assert _count(session, Lot, Lot.external_lot_id == fallback_lot_id) == 1
            assert _count(session, Unit, Unit.external_unit_id == client.unit_id) == 1
    finally:
        with Session(test_engine) as session:
            unit_ids = select(Unit.id).where(Unit.external_unit_id == client.unit_id)
            session.execute(
                delete(CustodyEvent).where(CustodyEvent.unit_id.in_(unit_ids))
            )
            session.execute(delete(Unit).where(Unit.external_unit_id == client.unit_id))
            session.execute(delete(Lot).where(Lot.external_lot_id == fallback_lot_id))
            session.execute(
                delete(Location).where(
                    Location.code.in_((client.store_1, client.store_2))
                )
            )
            session.execute(delete(Product).where(Product.sku == client.sku))
            session.commit()


def test_sync_preserves_local_reservation_while_unit_remains_available(
    db_session,
) -> None:
    suffix = uuid4().hex
    product = Product(
        sku=f"RESERVED-{suffix}",
        name="Reserved kit",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    location = Location(
        code=f"RESERVED-LOCATION-{suffix}",
        name="Sellable warehouse",
        is_refrigerated=False,
    )
    db_session.add_all([product, location])
    db_session.flush()
    expires_at = datetime.now(UTC) + timedelta(days=30)
    lot = Lot(
        external_lot_id=f"RESERVED-LOT-{suffix}",
        product_id=product.id,
        expires_at=expires_at,
        origin=LotOrigin.OWN_PRODUCTION,
    )
    db_session.add(lot)
    db_session.flush()
    unit = Unit(
        external_unit_id=f"RESERVED-UNIT-{suffix}",
        lot_id=lot.id,
        current_location_id=location.id,
        status="reserved",
        effective_expires_at=expires_at,
    )
    db_session.add(unit)
    db_session.flush()

    upsert_units(
        db_session,
        (
            UnitData(
                external_unit_id=unit.external_unit_id,
                lot_external_id=lot.external_lot_id,
                location_code=location.code,
                status="available",
                effective_expires_at=expires_at,
            ),
        ),
        {lot.external_lot_id: lot},
        {location.code: location},
    )

    assert unit.status == "reserved"


def test_incomplete_inventory_group_does_not_hide_unseen_units(db_session) -> None:
    suffix = uuid4().hex
    product = Product(
        sku=f"LARGE-{suffix}",
        name="Large inventory",
        category=ProductCategory.INSUMO,
        batch_size=1,
        requires_refrigeration=False,
    )
    location = Location(
        code=f"BUFFER-{suffix}",
        name="Buffer",
        is_refrigerated=True,
    )
    db_session.add_all([product, location])
    db_session.flush()
    lot = Lot(
        external_lot_id=f"LARGE-LOT-{suffix}",
        product_id=product.id,
        origin=LotOrigin.FARMA_CENTRAL,
    )
    db_session.add(lot)
    db_session.flush()
    unit = Unit(
        external_unit_id=f"LARGE-UNIT-{suffix}",
        lot_id=lot.id,
        current_location_id=location.id,
        status="available",
        effective_expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(unit)
    db_session.flush()

    upsert_units(
        db_session,
        [],
        {},
        {},
        incomplete_groups={(location.code, product.sku)},
    )

    assert unit.status == "available"


def _count(db_session, model, criterion) -> int:
    return db_session.scalar(select(func.count()).select_from(model).where(criterion))
