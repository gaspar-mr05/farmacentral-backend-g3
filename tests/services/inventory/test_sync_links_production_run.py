from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import (
    CustodyEvent,
    CustodyEventType,
    Lot,
    LotOrigin,
    Product,
    ProductCategory,
)
from app.schemas.inventory import InventoryData
from app.schemas.locations import LocationData
from app.schemas.lots import LotData
from app.schemas.units import UnitData
from app.services.inventory.sync import InventorySyncService, sync_inventory
from app.services.production.runs import start_production_run


def test_sync_groups_unbatched_output_units_and_links_pending_run(db_session):
    suffix = uuid4().hex
    sku = f"OUTPUT-{suffix}"
    location_code = f"PACKAGING-{suffix}"
    now = datetime.now(UTC)
    run = start_production_run(
        db_session,
        requested_at=now,
        expected_sku=sku,
        expected_quantity=2,
        available_at=now,
    )
    data = InventoryData(
        products=(),
        locations=(LocationData(location_code, "Packaging", False),),
        lots=(
            LotData(
                external_lot_id=f"unreported:UNIT-1-{suffix}",
                product_sku=sku,
                expires_at=now + timedelta(days=30),
                origin=LotOrigin.FARMA_CENTRAL,
            ),
            LotData(
                external_lot_id=f"unreported:UNIT-2-{suffix}",
                product_sku=sku,
                expires_at=now + timedelta(days=30),
                origin=LotOrigin.FARMA_CENTRAL,
            ),
        ),
        units=(
            UnitData(
                external_unit_id=f"UNIT-1-{suffix}",
                lot_external_id=f"unreported:UNIT-1-{suffix}",
                location_code=location_code,
                status="available",
                effective_expires_at=now + timedelta(days=30),
            ),
            UnitData(
                external_unit_id=f"UNIT-2-{suffix}",
                lot_external_id=f"unreported:UNIT-2-{suffix}",
                location_code=location_code,
                status="available",
                effective_expires_at=now + timedelta(days=30),
            ),
        ),
    )

    # The product is normally inserted from catalog data before lots.
    product = Product(
        sku=sku,
        name="Output",
        category=ProductCategory.ACONDICIONADO,
        batch_size=2,
        requires_refrigeration=False,
    )
    db_session.add(product)
    db_session.flush()

    sync_inventory(db_session, data)

    db_session.refresh(run)
    assert run.completed_at is not None
    assert run.output_lot is not None
    assert run.output_lot.origin is LotOrigin.OWN_PRODUCTION
    assert len(run.output_lot.units) == 2
    assert {unit.lot_id for unit in run.output_lot.units} == {run.output_lot_id}
    produced_events = db_session.scalar(
        select(func.count())
        .select_from(CustodyEvent)
        .where(CustodyEvent.event_type == CustodyEventType.PRODUCED)
    )
    assert produced_events == 2

    sync_inventory(db_session, data)

    db_session.refresh(run)
    assert len(run.output_lot.units) == 2
    empty_fallback_lots = db_session.scalar(
        select(func.count())
        .select_from(Lot)
        .where(Lot.external_lot_id.startswith("unreported:"))
    )
    assert empty_fallback_lots == 0


def test_sync_links_batched_output_units_to_pending_run(db_session):
    suffix = uuid4().hex
    sku = f"OUTPUT-{suffix}"
    location_code = f"PACKAGING-{suffix}"
    external_lot_id = f"LOT-{suffix}"
    now = datetime.now(UTC)
    product = Product(
        sku=sku,
        name="Output",
        category=ProductCategory.ACONDICIONADO,
        batch_size=2,
        requires_refrigeration=False,
    )
    db_session.add(product)
    db_session.flush()
    run = start_production_run(
        db_session,
        requested_at=now,
        expected_sku=sku,
        expected_quantity=2,
        available_at=now,
    )
    data = InventoryData(
        products=(),
        locations=(LocationData(location_code, "Packaging", False),),
        lots=(
            LotData(
                external_lot_id=external_lot_id,
                product_sku=sku,
                expires_at=now + timedelta(days=30),
                origin=LotOrigin.FARMA_CENTRAL,
            ),
        ),
        units=(
            UnitData(
                external_unit_id=f"UNIT-1-{suffix}",
                lot_external_id=external_lot_id,
                location_code=location_code,
                status="available",
                effective_expires_at=now + timedelta(days=30),
            ),
            UnitData(
                external_unit_id=f"UNIT-2-{suffix}",
                lot_external_id=external_lot_id,
                location_code=location_code,
                status="available",
                effective_expires_at=now + timedelta(days=30),
            ),
        ),
    )

    sync_inventory(db_session, data)

    db_session.refresh(run)
    assert run.completed_at is not None
    assert run.output_lot is not None
    assert run.output_lot.external_lot_id == external_lot_id
    assert run.output_lot.origin is LotOrigin.OWN_PRODUCTION
    assert len(run.output_lot.units) == 2

    # Later inventory synchronizations still report every visible lot with the
    # generic external origin. Local production evidence must remain authoritative.
    sync_inventory(db_session, data)

    db_session.refresh(run.output_lot)
    assert run.output_lot.origin is LotOrigin.OWN_PRODUCTION


@pytest.mark.anyio
async def test_service_finds_pending_output_before_inventory_summary_updates(
    db_session,
):
    suffix = uuid4().hex
    sku = f"OUTPUT-{suffix}"
    location_code = f"PACKAGING-{suffix}"
    now = datetime.now(UTC)

    class EventuallyConsistentClient:
        async def get_available_products(self):
            return [
                {
                    "sku": sku,
                    "name": "Output",
                    "production": {"batch": 1, "at": "packaging"},
                    "sellable": True,
                    "components": [{"sku": "INPUT", "req": 1}],
                }
            ]

        async def get_spaces(self):
            return [{"_id": location_code, "packaging": True}]

        async def get_space_inventory(self, store_id):
            return []

        async def get_space_products(self, store_id, product_sku, *, limit=None):
            assert product_sku == sku
            return [
                {
                    "_id": f"UNIT-{suffix}",
                    "sku": sku,
                    "store": store_id,
                    "expiresAt": (now + timedelta(days=30)).isoformat(),
                }
            ]

    product = Product(
        sku=sku,
        name="Output",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    db_session.add(product)
    db_session.flush()
    run = start_production_run(
        db_session,
        requested_at=now,
        expected_sku=sku,
        expected_quantity=1,
        available_at=now,
    )

    await InventorySyncService(EventuallyConsistentClient(), db_session).synchronize()

    db_session.refresh(run)
    assert run.output_lot is not None
    assert run.output_lot.origin is LotOrigin.OWN_PRODUCTION
    assert len(run.output_lot.units) == 1
