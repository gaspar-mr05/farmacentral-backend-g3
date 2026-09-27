from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Location
from app.schemas.locations import LocationData


@dataclass(frozen=True)
class LocationUpsertResult:
    created: int
    updated: int
    by_code: dict[str, Location]


def upsert_locations(
    session: Session, records: Iterable[LocationData]
) -> LocationUpsertResult:
    locations = {
        location.code: location for location in session.scalars(select(Location))
    }
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
            continue

        changed = (
            location.name != record.name
            or location.is_refrigerated != record.is_refrigerated
        )
        location.name = record.name
        location.is_refrigerated = record.is_refrigerated
        updated += int(changed)

    return LocationUpsertResult(created, updated, locations)
