import uuid

from sqlalchemy.orm import Session

from app.models import CustodyEvent, CustodyEventType


def create_custody_event(
    session: Session,
    *,
    unit_id: uuid.UUID,
    event_type: CustodyEventType,
    from_location_id: uuid.UUID | None,
    to_location_id: uuid.UUID | None,
) -> CustodyEvent:
    event = CustodyEvent(
        unit_id=unit_id,
        event_type=event_type,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
    )
    session.add(event)
    return event
