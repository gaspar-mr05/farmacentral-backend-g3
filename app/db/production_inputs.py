import uuid

from sqlalchemy.orm import Session

from app.models import ProductionInput, ProductionInputUnit


def create_production_input(
    session: Session,
    *,
    production_run_id: uuid.UUID,
    input_lot_id: uuid.UUID,
    quantity_consumed: int,
) -> ProductionInput:
    production_input = ProductionInput(
        production_run_id=production_run_id,
        input_lot_id=input_lot_id,
        quantity_consumed=quantity_consumed,
    )
    session.add(production_input)
    session.flush()
    return production_input


def create_production_input_unit(
    session: Session, *, production_input_id: uuid.UUID, unit_id: uuid.UUID
) -> ProductionInputUnit:
    input_unit = ProductionInputUnit(production_input_id=production_input_id, unit_id=unit_id)
    session.add(input_unit)
    return input_unit