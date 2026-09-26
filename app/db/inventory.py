from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Location, Lot, Product, Unit
from app.schemas.inventory import (
    InventoryData,
    LocationData,
    LotData,
    ProductData,
    UnitData,
)


@dataclass(frozen=True)
class EntityChanges:
    created: int = 0
    updated: int = 0


@dataclass(frozen=True)
class InventorySyncResult:
    products: EntityChanges
    locations: EntityChanges
    lots: EntityChanges
    units: EntityChanges


def sync_inventory(session: Session, data: InventoryData) -> InventorySyncResult:
    product_changes, products = _upsert_products(session, data.products)
    location_changes, locations = _upsert_locations(session, data.locations)
    session.flush()
    lot_changes, lots = _upsert_lots(session, data.lots, products)
    session.flush()
    unit_changes = _upsert_units(session, data.units, lots, locations)
    session.flush()
    return InventorySyncResult(
        products=product_changes,
        locations=location_changes,
        lots=lot_changes,
        units=unit_changes,
    )


def _upsert_products(
    session: Session, records: Iterable[ProductData]
) -> tuple[EntityChanges, dict[str, Product]]:
    products = {item.sku: item for item in session.scalars(select(Product)).all()}
    created = updated = 0
    for record in records:
        product = products.get(record.sku)
        if product is None:
            product = Product(
                sku=record.sku,
                name=record.name,
                category=record.category,
                batch_size=record.batch_size,
                requires_refrigeration=record.requires_refrigeration,
            )
            session.add(product)
            products[record.sku] = product
            created += 1
        else:
            updated += _update(
                product,
                name=record.name,
                category=record.category,
                batch_size=record.batch_size,
                requires_refrigeration=record.requires_refrigeration,
            )
    return EntityChanges(created, updated), products


def _upsert_locations(
    session: Session, records: Iterable[LocationData]
) -> tuple[EntityChanges, dict[str, Location]]:
    locations = {item.code: item for item in session.scalars(select(Location)).all()}
    created = updated = 0
    for record in records:
        location = locations.get(record.code)
        if location is None:
            location = Location(
                code=record.code,
                name=record.name,
                is_refrigerated=record.is_refrigerated,
            )
            session.add(location)
            locations[record.code] = location
            created += 1
        else:
            updated += _update(
                location,
                name=record.name,
                is_refrigerated=record.is_refrigerated,
            )
    return EntityChanges(created, updated), locations


def _upsert_lots(
    session: Session,
    records: Iterable[LotData],
    products: dict[str, Product],
) -> tuple[EntityChanges, dict[str, Lot]]:
    lots = {item.external_lot_id: item for item in session.scalars(select(Lot)).all()}
    created = updated = 0
    for record in records:
        product = products[record.product_sku]
        lot = lots.get(record.external_lot_id)
        if lot is None:
            lot = Lot(
                external_lot_id=record.external_lot_id,
                product_id=product.id,
                expires_at=record.expires_at,
                origin=record.origin,
            )
            session.add(lot)
            lots[record.external_lot_id] = lot
            created += 1
        else:
            updated += _update(
                lot,
                product_id=product.id,
                expires_at=record.expires_at,
                origin=record.origin,
            )
    return EntityChanges(created, updated), lots


def _upsert_units(
    session: Session,
    records: Iterable[UnitData],
    lots: dict[str, Lot],
    locations: dict[str, Location],
) -> EntityChanges:
    records = tuple(records)
    units = {
        item.external_unit_id: item for item in session.scalars(select(Unit)).all()
    }
    created = updated = 0
    for record in records:
        lot = lots[record.lot_external_id]
        location = locations[record.location_code]
        unit = units.get(record.external_unit_id)
        if unit is None:
            unit = Unit(
                external_unit_id=record.external_unit_id,
                lot_id=lot.id,
                current_location_id=location.id,
                status=record.status,
            )
            session.add(unit)
            units[record.external_unit_id] = unit
            created += 1
        else:
            updated += _update(
                unit,
                lot_id=lot.id,
                current_location_id=location.id,
                status=record.status,
            )

    visible_ids = {record.external_unit_id for record in records}
    for external_id, unit in units.items():
        if external_id not in visible_ids and unit.status == "available":
            unit.status = "unavailable"
            updated += 1
    return EntityChanges(created, updated)


def _update(entity: object, **values: object) -> int:
    changed = False
    for attribute, value in values.items():
        if getattr(entity, attribute) != value:
            setattr(entity, attribute, value)
            changed = True
    return int(changed)
