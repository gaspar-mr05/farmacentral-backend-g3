from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Location, Lot, LotOrigin, Product, ProductCategory, Unit


@dataclass(frozen=True)
class MovementScenario:
    product_id: str
    origin_code: str
    destination_code: str
    unit: Unit
    origin: Location
    destination: Location


def create_movement_scenario(session: Session) -> MovementScenario:
    suffix = uuid4().hex
    product = Product(
        sku=f"MOVEMENT-SKU-{suffix}",
        name="Movement test product",
        category=ProductCategory.INSUMO,
        batch_size=10,
        requires_refrigeration=False,
    )
    origin = Location(
        code=f"movement-origin-{suffix}",
        name="Movement origin",
        is_refrigerated=False,
    )
    destination = Location(
        code=f"movement-destination-{suffix}",
        name="Movement destination",
        is_refrigerated=True,
    )
    session.add_all([product, origin, destination])
    session.flush()

    lot = Lot(
        external_lot_id=f"MOVEMENT-LOT-{suffix}",
        product_id=product.id,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        origin=LotOrigin.FARMA_CENTRAL,
    )
    session.add(lot)
    session.flush()

    product_id = f"movement-unit-{suffix}"
    unit = Unit(
        external_unit_id=product_id,
        lot_id=lot.id,
        current_location_id=origin.id,
        status="available",
    )
    session.add(unit)
    session.commit()

    return MovementScenario(
        product_id=product_id,
        origin_code=origin.code,
        destination_code=destination.code,
        unit=unit,
        origin=origin,
        destination=destination,
    )
