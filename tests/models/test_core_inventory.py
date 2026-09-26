# tests/models/test_core_inventory.py
import uuid
from datetime import UTC, datetime, timedelta

from app.models import Location, Lot, LotOrigin, Product, ProductCategory, Unit


def test_can_create_and_traverse_inventory_relationships(db_session):
    test_id = uuid.uuid4().hex[:8]
    sku = f"API-AMOXI-500-{test_id}"
    product = Product(
        sku=sku,
        name="Amoxicilina 500mg (principio activo)",
        category=ProductCategory.INSUMO,
        batch_size=50,
        requires_refrigeration=False,
    )
    db_session.add(product)
    db_session.flush()  # asigna el id sin hacer commit

    location = Location(
        code="BODEGA_PRINCIPAL",
        name="Bodega principal",
        is_refrigerated=False,
    )
    db_session.add(location)
    db_session.flush()

    lot = Lot(
        external_lot_id=f"L-TEST-{uuid.uuid4().hex[:8]}",
        product_id=product.id,
        expires_at=datetime.now(UTC) + timedelta(days=365),
        origin=LotOrigin.FARMA_CENTRAL,
    )
    db_session.add(lot)
    db_session.flush()

    unit = Unit(
        external_unit_id=f"U-TEST-{uuid.uuid4().hex[:8]}",
        lot_id=lot.id,
        current_location_id=location.id,
        status="available",
    )
    db_session.add(unit)
    db_session.flush()

    # --- Lectura y navegación de relaciones ---
    db_session.refresh(product)
    db_session.refresh(lot)

    assert len(product.lots) == 1
    assert product.lots[0].id == lot.id

    assert len(lot.units) == 1
    fetched_unit = lot.units[0]
    assert fetched_unit.id == unit.id

    # navegar hacia atrás: unidad -> lote -> producto
    assert fetched_unit.lot.product.sku == sku

    # navegar hacia la ubicación actual
    assert fetched_unit.current_location.code == "BODEGA_PRINCIPAL"
    assert fetched_unit.current_location.is_refrigerated is False
