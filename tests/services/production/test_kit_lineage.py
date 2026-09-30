# tests/services/production/test_kit_lineage.py
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Location,
    Lot,
    LotOrigin,
    Product,
    ProductCategory,
    ProductionInput,
    ProductionRun,
    Unit,
)
from app.services.production.consumption import consume_units_for_run
from app.services.production.runs import start_production_run
from tests.conftest import db_session


def test_kit_production_records_multilevel_lineage(db_session: Session) -> None:
    now = datetime.now(UTC)

    # 1. Crear ubicación obligatoria para las unidades
    location = Location(
        code="PKG-01",
        name="Área de Acondicionamiento",
    )
    db_session.add(location)
    db_session.flush()

    # 2. Crear producto intermedio (Blíster) y Kit final
    blister_product = Product(
        sku="BLI-AMOXI-500",
        name="Blíster amoxicilina",
        category=ProductCategory.ACONDICIONADO,
        batch_size=3,
        requires_refrigeration=False,
    )
    kit_product = Product(
        sku="KIT-RESP-ADULTO",
        name="Kit Respiratorio Adulto",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    db_session.add_all([blister_product, kit_product])
    db_session.flush()

    # 3. Crear lote usando LotOrigin.FARMA_CENTRAL
    blister_lot = Lot(
        product_id=blister_product.id,
        external_lot_id="L-BLI-001",
        origin=LotOrigin.FARMA_CENTRAL,
        expires_at=now,
    )
    db_session.add(blister_lot)
    db_session.flush()

    # 4. Crear unidad con location y effective_expires_at
    blister_unit = Unit(
        external_unit_id="U-BLI-001",
        lot_id=blister_lot.id,
        current_location_id=location.id,
        status="available",
        effective_expires_at=now,
    )
    db_session.add(blister_unit)
    db_session.flush()

    # 5. Registrar ejecución de producción del Kit
    run = start_production_run(
        db_session, requested_at=now, expected_sku="KIT-RESP-ADULTO"
    )
    consume_units_for_run(
        db_session,
        production_run_id=run.id,
        input_units_by_lot={blister_lot.id: [blister_unit]},
    )
    db_session.commit()

    # 6. Verificar trazabilidad/linaje
    saved_run = db_session.scalar(
        select(ProductionRun).where(ProductionRun.id == run.id)
    )
    assert saved_run is not None
    assert saved_run.expected_sku == "KIT-RESP-ADULTO"

    inputs = db_session.scalars(
        select(ProductionInput).where(ProductionInput.production_run_id == run.id)
    ).all()
    assert len(inputs) == 1
    assert inputs[0].input_lot_id == blister_lot.id
    assert len(inputs[0].units) == 1  # <--- Cambiado a .units