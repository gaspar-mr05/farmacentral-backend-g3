import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Lot, LotOrigin, ProductionRun, Product

from sqlalchemy import select



def create_production_run(
    session: Session, *, requested_at: datetime, expected_sku: str
) -> ProductionRun:
    run = ProductionRun(requested_at=requested_at, expected_sku=expected_sku)
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


def find_pending_run_for_sku(session: Session, *, sku: str) -> ProductionRun | None:

    statement = (
        select(ProductionRun)
        .where(ProductionRun.completed_at.is_(None))
        .order_by(ProductionRun.requested_at.asc())
    )
    return session.scalars(statement).first()


def find_pending_run_for_sku(session: Session, *, sku: str) -> ProductionRun | None:
    statement = (
        select(ProductionRun)
        .where(ProductionRun.completed_at.is_(None), ProductionRun.expected_sku == sku)
        .order_by(ProductionRun.requested_at.asc())
    )
    """Busca la corrida de producción más antigua sin completar cuyo
    lote de salida (aún no vinculado) corresponde a este SKU.

    Nota: como ProductionRun no guarda el SKU directamente (solo se sabe
    una vez que existe el output_lot), esta función depende de que quien
    la llame haya registrado el SKU esperado en algún lugar accesible.
    Ver nota de diseño abajo.
    """
    return session.scalars(statement).first()