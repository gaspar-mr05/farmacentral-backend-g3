import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Location, Lot, Unit
from app.schemas.units import UnitData


@dataclass(frozen=True)
class UnitLocationChange:
    unit: Unit
    from_location_id: uuid.UUID | None
    to_location_id: uuid.UUID

    @property
    def is_received(self) -> bool:
        return self.from_location_id is None


@dataclass(frozen=True)
class UnitUpsertResult:
    created: int
    updated: int
    location_changes: tuple[UnitLocationChange, ...]
    created_units: tuple[Unit, ...]


def get_unit_by_external_id(
    session: Session,
    external_unit_id: str,
    *,
    for_update: bool = False,
) -> Unit | None:
    statement = select(Unit).where(Unit.external_unit_id == external_unit_id)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def upsert_units(
    session: Session,
    records: Iterable[UnitData],
    lots_by_external_id: dict[str, Lot],
    locations_by_code: dict[str, Location],
) -> UnitUpsertResult:
    records = tuple(records)
    units = {
        unit.external_unit_id: unit for unit in session.scalars(select(Unit)).all()
    }
    location_changes: list[UnitLocationChange] = []
    created_units: list[Unit] = []
    created = updated = 0

    for record in records:
        incoming_lot = lots_by_external_id[record.lot_external_id]
        location = locations_by_code[record.location_code]
        unit = units.get(record.external_unit_id)

        if unit is None:
            unit = Unit(
                external_unit_id=record.external_unit_id,
                lot_id=incoming_lot.id,
                current_location_id=location.id,
                status=record.status,
                effective_expires_at=record.effective_expires_at,
            )
            session.add(unit)
            session.flush()
            units[record.external_unit_id] = unit
            created_units.append(unit)
            location_changes.append(UnitLocationChange(unit, None, location.id))
            created += 1
            continue

        lot = (
            unit.lot
            if record.lot_external_id.startswith("unreported:")
            else incoming_lot
        )
        synchronized_status = (
            "reserved"
            if unit.status == "reserved" and record.status == "available"
            else record.status
        )
        previous_location_id = unit.current_location_id
        changed = (
            unit.lot_id != lot.id
            or unit.current_location_id != location.id
            or unit.status != synchronized_status
            or unit.effective_expires_at != record.effective_expires_at
        )
        unit.lot_id = lot.id
        unit.current_location_id = location.id
        unit.status = synchronized_status
        unit.effective_expires_at = record.effective_expires_at
        updated += int(changed)

        if previous_location_id != location.id:
            location_changes.append(
                UnitLocationChange(unit, previous_location_id, location.id)
            )

    visible_ids = {record.external_unit_id for record in records}
    for external_id, unit in units.items():
        if external_id not in visible_ids and unit.status == "available":
            unit.status = "unavailable"
            updated += 1

    return UnitUpsertResult(
        created,
        updated,
        tuple(location_changes),
        tuple(created_units),
    )


def set_current_location(unit: Unit, location_id: uuid.UUID) -> None:
    unit.current_location_id = location_id


def set_status(unit: Unit, status: str) -> None:
    unit.status = status
