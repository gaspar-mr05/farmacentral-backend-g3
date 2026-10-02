from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import (
    Location,
    Lot,
    LotOrigin,
    Product,
    ProductCategory,
    ProductionInput,
    ProductionInputUnit,
    ProductionRun,
    Unit,
)


@dataclass(frozen=True)
class TraceabilityScenario:
    raw_lot_id: UUID
    intermediate_lot_id: UUID
    kit_lot_id: UUID
    raw_lot_external_id: str
    intermediate_lot_external_id: str
    kit_lot_external_id: str
    kit_unit_external_id: str
    location_code: str


def create_traceability_scenario(session: Session) -> TraceabilityScenario:
    suffix = uuid4().hex
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=90)

    location = Location(
        code=f"TRACE-{suffix}",
        name="Área de trazabilidad",
        is_refrigerated=False,
    )
    raw_product = Product(
        sku=f"RAW-{suffix}",
        name="Insumo trazable",
        category=ProductCategory.INSUMO,
        batch_size=1,
        requires_refrigeration=False,
    )
    intermediate_product = Product(
        sku=f"INTERMEDIATE-{suffix}",
        name="Acondicionado trazable",
        category=ProductCategory.ACONDICIONADO,
        batch_size=1,
        requires_refrigeration=False,
    )
    kit_product = Product(
        sku=f"KIT-{suffix}",
        name="Kit trazable",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    session.add_all([location, raw_product, intermediate_product, kit_product])
    session.flush()

    raw_lot = Lot(
        external_lot_id=f"RAW-LOT-{suffix}",
        product_id=raw_product.id,
        expires_at=expires_at,
        origin=LotOrigin.FARMA_CENTRAL,
    )
    intermediate_lot = Lot(
        external_lot_id=f"INTERMEDIATE-LOT-{suffix}",
        product_id=intermediate_product.id,
        expires_at=expires_at,
        origin=LotOrigin.OWN_PRODUCTION,
    )
    kit_lot = Lot(
        external_lot_id=f"KIT-LOT-{suffix}",
        product_id=kit_product.id,
        expires_at=expires_at,
        origin=LotOrigin.OWN_PRODUCTION,
    )
    session.add_all([raw_lot, intermediate_lot, kit_lot])
    session.flush()

    raw_unit = Unit(
        external_unit_id=f"RAW-UNIT-{suffix}",
        lot_id=raw_lot.id,
        current_location_id=location.id,
        status="consumed",
        effective_expires_at=expires_at,
    )
    intermediate_unit = Unit(
        external_unit_id=f"INTERMEDIATE-UNIT-{suffix}",
        lot_id=intermediate_lot.id,
        current_location_id=location.id,
        status="consumed",
        effective_expires_at=expires_at,
    )
    kit_unit = Unit(
        external_unit_id=f"KIT-UNIT-{suffix}",
        lot_id=kit_lot.id,
        current_location_id=location.id,
        status="available",
        effective_expires_at=expires_at,
    )
    session.add_all([raw_unit, intermediate_unit, kit_unit])
    session.flush()

    intermediate_run = ProductionRun(
        output_lot_id=intermediate_lot.id,
        expected_sku=intermediate_product.sku,
        expected_quantity=1,
        requested_at=now,
        completed_at=now,
    )
    kit_run = ProductionRun(
        output_lot_id=kit_lot.id,
        expected_sku=kit_product.sku,
        expected_quantity=1,
        requested_at=now,
        completed_at=now,
    )
    session.add_all([intermediate_run, kit_run])
    session.flush()

    raw_input = ProductionInput(
        production_run_id=intermediate_run.id,
        input_lot_id=raw_lot.id,
        quantity_consumed=1,
    )
    intermediate_input = ProductionInput(
        production_run_id=kit_run.id,
        input_lot_id=intermediate_lot.id,
        quantity_consumed=1,
    )
    session.add_all([raw_input, intermediate_input])
    session.flush()
    session.add_all(
        [
            ProductionInputUnit(
                production_input_id=raw_input.id,
                unit_id=raw_unit.id,
            ),
            ProductionInputUnit(
                production_input_id=intermediate_input.id,
                unit_id=intermediate_unit.id,
            ),
        ]
    )
    session.flush()

    return TraceabilityScenario(
        raw_lot_id=raw_lot.id,
        intermediate_lot_id=intermediate_lot.id,
        kit_lot_id=kit_lot.id,
        raw_lot_external_id=raw_lot.external_lot_id,
        intermediate_lot_external_id=intermediate_lot.external_lot_id,
        kit_lot_external_id=kit_lot.external_lot_id,
        kit_unit_external_id=kit_unit.external_unit_id,
        location_code=location.code,
    )
