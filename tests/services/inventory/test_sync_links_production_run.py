# tests/services/inventory/test_sync_links_production_run.py
from datetime import datetime, timezone

from app.models import LotOrigin, Product, ProductCategory
from app.schemas.inventory import InventoryData
from app.schemas.lots import LotData
from app.services.inventory.sync import sync_inventory
from app.services.production.runs import start_production_run


def test_sync_links_new_output_lot_to_pending_run(db_session):
    output_product = Product(
        sku="BLI-AMOXI-500", name="Blíster amoxicilina",
        category=ProductCategory.ACONDICIONADO, batch_size=3, requires_refrigeration=False,
    )
    db_session.add(output_product)
    db_session.flush()

    now = datetime.now(timezone.utc)
    run = start_production_run(db_session, requested_at=now, expected_sku="BLI-AMOXI-500")

    data = InventoryData(
        products=(),
        locations=(),
        lots=(
            LotData(
                external_lot_id="L-BLI-REAL-001",
                product_sku="BLI-AMOXI-500",
                expires_at=None,
                origin=LotOrigin.OWN_PRODUCTION,
            ),
        ),
        units=(),
    )

    sync_inventory(db_session, data)

    db_session.refresh(run)
    assert run.completed_at is not None
    assert run.output_lot_id is not None