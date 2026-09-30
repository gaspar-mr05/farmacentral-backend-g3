# tests/models/test_production_run.py
import uuid
from datetime import datetime, timedelta, timezone

from app.models import (
    Lot, LotOrigin, Location, Product, ProductCategory,
    ProductionInput, ProductionInputUnit, ProductionRun, Unit,
)


def test_production_run_links_multiple_input_lots_to_one_output_lot(db_session):
    location = Location(code="ACONDICIONAMIENTO", name="Área de acondicionamiento", is_refrigerated=False)
    db_session.add(location)
    db_session.flush()

    api_product = Product(sku="API-AMOXI-500", name="API Amoxicilina", category=ProductCategory.INSUMO, batch_size=50, requires_refrigeration=False)
    exc_product = Product(sku="EXC-LACTOSA-DC", name="Excipiente lactosa", category=ProductCategory.INSUMO, batch_size=50, requires_refrigeration=False)
    output_product = Product(sku="BLI-AMOXI-500", name="Blíster amoxicilina", category=ProductCategory.ACONDICIONADO, batch_size=3, requires_refrigeration=False)
    db_session.add_all([api_product, exc_product, output_product])
    db_session.flush()

    api_lot = Lot(external_lot_id="L-API-001", product_id=api_product.id, origin=LotOrigin.FARMA_CENTRAL)
    exc_lot = Lot(external_lot_id="L-EXC-001", product_id=exc_product.id, origin=LotOrigin.FARMA_CENTRAL)
    db_session.add_all([api_lot, exc_lot])
    db_session.flush()

    now = datetime.now(timezone.utc)
    far_future = now + timedelta(days=365)

    api_units = [
        Unit(
            external_unit_id=f"U-API-{i}",
            lot_id=api_lot.id,
            current_location_id=location.id,
            status="consumed",
            effective_expires_at=far_future,
        )
        for i in range(12)
    ]
    exc_units = [
        Unit(
            external_unit_id=f"U-EXC-{i}",
            lot_id=exc_lot.id,
            current_location_id=location.id,
            status="consumed",
            effective_expires_at=far_future,
        )
        for i in range(8)
    ]
    db_session.add_all(api_units + exc_units)
    db_session.flush()

    output_lot = Lot(
        external_lot_id="L-BLI-001", product_id=output_product.id, origin=LotOrigin.OWN_PRODUCTION
    )
    db_session.add(output_lot)
    db_session.flush()

    run = ProductionRun(
        output_lot_id=output_lot.id,
        expected_sku="KIT-RESP-ADULTO",
        requested_at=now,
        completed_at=now,
    )
    db_session.add(run)
    db_session.flush()

    api_input = ProductionInput(production_run_id=run.id, input_lot_id=api_lot.id, quantity_consumed=12)
    exc_input = ProductionInput(production_run_id=run.id, input_lot_id=exc_lot.id, quantity_consumed=8)
    db_session.add_all([api_input, exc_input])
    db_session.flush()

    db_session.add_all([ProductionInputUnit(production_input_id=api_input.id, unit_id=u.id) for u in api_units])
    db_session.add_all([ProductionInputUnit(production_input_id=exc_input.id, unit_id=u.id) for u in exc_units])
    db_session.flush()

    db_session.refresh(output_lot)
    assert output_lot.produced_by.id == run.id
    consumed_lot_ids = {i.input_lot_id for i in output_lot.produced_by.inputs}
    assert consumed_lot_ids == {api_lot.id, exc_lot.id}

    api_consumed_units = next(
        i for i in output_lot.produced_by.inputs if i.input_lot_id == api_lot.id
    ).units
    assert len(api_consumed_units) == 12

    db_session.refresh(api_lot)
    assert len(api_lot.consumed_in) == 1
    assert api_lot.consumed_in[0].production_run.output_lot_id == output_lot.id