# app/services/production/runs.py
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.production_runs import complete_production_run, create_production_run
from app.models import Lot, ProductionRun


def start_production_run(
    session: Session,
    *,
    requested_at: datetime,
    expected_sku: str,
    expected_quantity: int,
    available_at: datetime,
) -> ProductionRun:
    """Registra el inicio de una producción, justo después de que se confirma
    la solicitud a Farma Central. El lote de salida aún no existe."""
    return create_production_run(
        session,
        requested_at=requested_at,
        expected_sku=expected_sku,
        expected_quantity=expected_quantity,
        available_at=available_at,
    )


def finish_production_run(
    session: Session,
    *,
    run: ProductionRun,
    output_lot_external_id: str,
    output_product_id: uuid.UUID,
    completed_at: datetime,
) -> Lot:
    """Vincula el lote de salida a la producción una vez que Farma Central
    confirma la recepción (detectado vía sincronización de inventario)."""
    return complete_production_run(
        session,
        run=run,
        output_lot_external_id=output_lot_external_id,
        output_product_id=output_product_id,
        completed_at=completed_at,
    )


def link_production_run_to_existing_lot(
    session: Session,
    *,
    run: ProductionRun,
    output_lot_id: uuid.UUID,
    completed_at: datetime,
) -> None:
    run.output_lot_id = output_lot_id
    run.completed_at = completed_at
    session.flush()
