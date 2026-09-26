# app/services/custody_service.py
import uuid
from sqlalchemy.orm import Session
from app.models import CustodyEvent, CustodyEventType, Unit


def log_custody_event(
    session: Session,
    *,
    unit_id: uuid.UUID,
    event_type: CustodyEventType,
    from_location_id: uuid.UUID | None,
    to_location_id: uuid.UUID | None,
) -> CustodyEvent:
    """Registra un evento de custodia sin tocar Unit.current_location_id
    (eso ya lo maneja quien llama, por ejemplo el upsert de sincronización)."""
    event = CustodyEvent(
        unit_id=unit_id,
        event_type=event_type,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
    )
    session.add(event)
    return event


# Estas dos quedan para cuando implementes movimientos "reales" fuera de sync
# (Paso 7 — product-movements), donde sí quieres que la función también
# actualice current_location_id como parte del mismo llamado.
def record_received(session: Session, *, unit: Unit, location_id: uuid.UUID) -> CustodyEvent:
    unit.current_location_id = location_id
    return log_custody_event(
        session,
        unit_id=unit.id,
        event_type=CustodyEventType.RECEIVED,
        from_location_id=None,
        to_location_id=location_id,
    )


def record_move(session: Session, *, unit: Unit, to_location_id: uuid.UUID) -> CustodyEvent:
    from_location_id = unit.current_location_id
    unit.current_location_id = to_location_id
    return log_custody_event(
        session,
        unit_id=unit.id,
        event_type=CustodyEventType.MOVED,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
    )