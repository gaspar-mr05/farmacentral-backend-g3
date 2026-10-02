from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Location, Lot, LotOrigin, Product, ProductCategory, Unit
from app.services.production.consumption import consume_units_for_run
from app.services.production.runs import finish_production_run, start_production_run


def test_kit_production_records_multilevel_lineage(db_session: Session) -> None:
    suffix = uuid4().hex
    now = datetime.now(UTC)
    location = Location(
        code=f"PACKAGING-{suffix}",
        name="Área de acondicionamiento",
        is_refrigerated=False,
    )
    raw_product = Product(
        sku=f"RAW-{suffix}",
        name="Raw material",
        category=ProductCategory.INSUMO,
        batch_size=1,
        requires_refrigeration=False,
    )
    intermediate_product = Product(
        sku=f"INTERMEDIATE-{suffix}",
        name="Intermediate",
        category=ProductCategory.ACONDICIONADO,
        batch_size=1,
        requires_refrigeration=False,
    )
    kit_product = Product(
        sku=f"KIT-{suffix}",
        name="Kit",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    db_session.add_all([location, raw_product, intermediate_product, kit_product])
    db_session.flush()

    raw_lot = Lot(
        product_id=raw_product.id,
        external_lot_id=f"RAW-LOT-{suffix}",
        origin=LotOrigin.FARMA_CENTRAL,
        expires_at=now + timedelta(days=30),
    )
    db_session.add(raw_lot)
    db_session.flush()
    raw_unit = Unit(
        external_unit_id=f"RAW-UNIT-{suffix}",
        lot_id=raw_lot.id,
        current_location_id=location.id,
        status="available",
        effective_expires_at=raw_lot.expires_at,
    )
    db_session.add(raw_unit)
    db_session.flush()

    intermediate_run = start_production_run(
        db_session,
        requested_at=now,
        expected_sku=intermediate_product.sku,
        expected_quantity=1,
        available_at=now,
    )
    consume_units_for_run(
        db_session,
        production_run_id=intermediate_run.id,
        input_units_by_lot={raw_lot.id: [raw_unit]},
    )
    intermediate_lot = finish_production_run(
        db_session,
        run=intermediate_run,
        output_lot_external_id=f"INTERMEDIATE-LOT-{suffix}",
        output_product_id=intermediate_product.id,
        completed_at=now,
    )
    intermediate_unit = Unit(
        external_unit_id=f"INTERMEDIATE-UNIT-{suffix}",
        lot_id=intermediate_lot.id,
        current_location_id=location.id,
        status="available",
        effective_expires_at=now + timedelta(days=20),
    )
    db_session.add(intermediate_unit)
    db_session.flush()

    kit_run = start_production_run(
        db_session,
        requested_at=now,
        expected_sku=kit_product.sku,
        expected_quantity=1,
        available_at=now,
    )
    consume_units_for_run(
        db_session,
        production_run_id=kit_run.id,
        input_units_by_lot={intermediate_lot.id: [intermediate_unit]},
    )
    kit_lot = finish_production_run(
        db_session,
        run=kit_run,
        output_lot_external_id=f"KIT-LOT-{suffix}",
        output_product_id=kit_product.id,
        completed_at=now,
    )

    kit_input_lot = kit_lot.produced_by.inputs[0].input_lot
    raw_ancestor_lot = kit_input_lot.produced_by.inputs[0].input_lot
    assert kit_input_lot.id == intermediate_lot.id
    assert raw_ancestor_lot.id == raw_lot.id
    assert kit_lot.origin is LotOrigin.OWN_PRODUCTION
    assert intermediate_lot.origin is LotOrigin.OWN_PRODUCTION
