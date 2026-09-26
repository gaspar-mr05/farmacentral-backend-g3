import uuid

from sqlalchemy.orm import Session

from app.db.custody_events import create_custody_event
from app.db.units import set_current_location
from app.models import CustodyEvent, CustodyEventType, Unit


def log_custody_event(
    session: Session,
    *,
    unit_id: uuid.UUID,
    event_type: CustodyEventType,
    from_location_id: uuid.UUID | None,
    to_location_id: uuid.UUID | None,
) -> CustodyEvent:
    """Registra un evento sin modificar la ubicación actual de la unidad."""
    return create_custody_event(
        session,
        unit_id=unit_id,
        event_type=event_type,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
    )


def record_received(
    session: Session, *, unit: Unit, location_id: uuid.UUID
) -> CustodyEvent:
    set_current_location(unit, location_id)
    return log_custody_event(
        session,
        unit_id=unit.id,
        event_type=CustodyEventType.RECEIVED,
        from_location_id=None,
        to_location_id=location_id,
    )


def record_move(
    session: Session, *, unit: Unit, to_location_id: uuid.UUID
) -> CustodyEvent:
    from_location_id = unit.current_location_id
    set_current_location(unit, to_location_id)
    return log_custody_event(
        session,
        unit_id=unit.id,
        event_type=CustodyEventType.MOVED,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
    )
