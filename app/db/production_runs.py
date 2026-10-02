import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lot, LotOrigin, ProductionRun


def create_production_run(
    session: Session,
    *,
    requested_at: datetime,
    expected_sku: str,
    expected_quantity: int,
    available_at: datetime,
) -> ProductionRun:
    run = ProductionRun(
        requested_at=requested_at,
        expected_sku=expected_sku,
        expected_quantity=expected_quantity,
        available_at=available_at,
    )
    session.add(run)
    session.flush()
    return run


def complete_production_run(
    session: Session,
    *,
    run: ProductionRun,
    output_lot_external_id: str,
    output_product_id: uuid.UUID,
    completed_at: datetime,
) -> Lot:
    output_lot = Lot(
        external_lot_id=output_lot_external_id,
        product_id=output_product_id,
        origin=LotOrigin.OWN_PRODUCTION,
    )
    session.add(output_lot)
    session.flush()

    run.output_lot_id = output_lot.id
    run.completed_at = completed_at
    session.flush()
    return output_lot


def find_pending_runs_for_sku(session: Session, *, sku: str) -> list[ProductionRun]:
    statement = (
        select(ProductionRun)
        .where(ProductionRun.completed_at.is_(None), ProductionRun.expected_sku == sku)
        .order_by(ProductionRun.requested_at.asc())
    )
    return list(session.scalars(statement))
