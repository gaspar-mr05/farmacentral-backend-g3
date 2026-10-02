# app/services/production/consumption.py
import uuid

from sqlalchemy.orm import Session

from app.db.production_inputs import (
    create_production_input,
    create_production_input_unit,
)
from app.db.units import set_status
from app.models import CustodyEventType, ProductionInput, Unit
from app.services.custody.events import log_custody_event


def consume_units_for_run(
    session: Session,
    *,
    production_run_id: uuid.UUID,
    input_units_by_lot: dict[uuid.UUID, list[Unit]],
) -> list[ProductionInput]:
    """Marca las unidades de insumo como consumidas, crea los registros de
    ProductionInput/ProductionInputUnit y registra el evento de custodia
    correspondiente."""
    production_inputs = []

    for lot_id, units in input_units_by_lot.items():
        production_input = create_production_input(
            session,
            production_run_id=production_run_id,
            input_lot_id=lot_id,
            quantity_consumed=len(units),
        )

        for unit in units:
            create_production_input_unit(
                session, production_input_id=production_input.id, unit_id=unit.id
            )
            set_status(unit, "consumed")
            log_custody_event(
                session,
                unit_id=unit.id,
                event_type=CustodyEventType.PRODUCTION_CONSUMED,
                from_location_id=unit.current_location_id,
                to_location_id=None,
            )

        production_inputs.append(production_input)

    session.flush()
    return production_inputs
