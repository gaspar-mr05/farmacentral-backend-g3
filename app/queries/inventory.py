from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Location, Lot, Product, Unit


def list_available_inventory(
    session: Session,
    *,
    sku: str | None = None,
    location_code: str | None = None,
) -> Sequence[Unit]:
    statement = (
        select(Unit)
        .join(Unit.lot)
        .join(Lot.product)
        .join(Unit.current_location)
        .options(
            joinedload(Unit.lot).joinedload(Lot.product),
            joinedload(Unit.current_location),
        )
        .where(Unit.status == "available")
        .order_by(
            Product.sku,
            Lot.expires_at,
            Unit.external_unit_id,
        )
    )

    if sku is not None:
        statement = statement.where(Product.sku == sku)

    if location_code is not None:
        statement = statement.where(Location.code == location_code)

    return session.scalars(statement).unique().all()
