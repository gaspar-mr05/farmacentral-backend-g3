from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Lot,
    OrderItem,
    OrderUnit,
    ProductionInput,
    ProductionInputUnit,
    ProductionRun,
    Unit,
)


def get_lot_with_units(session: Session, identifier: str) -> Lot | None:
    try:
        internal_id = UUID(identifier)
    except ValueError:
        internal_id = None
    statement = (
        select(Lot)
        .options(
            joinedload(Lot.product),
            selectinload(Lot.units).joinedload(Unit.current_location),
        )
        .where(
            or_(
                Lot.external_lot_id == identifier,
                Lot.id == internal_id,
            )
        )
    )
    return session.scalar(statement)


def list_lineage_inputs(
    session: Session, root_lot_id: UUID, *, upstream: bool
) -> Sequence[ProductionInput]:
    """Walk in PostgreSQL instead of issuing queries for each generation."""
    lineage = (
        select(Lot.id.label("lot_id"))
        .where(Lot.id == root_lot_id)
        .cte("traceability_lineage", recursive=True)
    )
    source = ProductionRun.output_lot_id if upstream else ProductionInput.input_lot_id
    target = ProductionInput.input_lot_id if upstream else ProductionRun.output_lot_id
    # UNION also terminates if historical data contains a cycle.
    lineage = lineage.union(
        select(target)
        .select_from(ProductionInput)
        .join(ProductionRun, ProductionRun.id == ProductionInput.production_run_id)
        .join(lineage, lineage.c.lot_id == source)
        .where(target.is_not(None))
    )
    return _list_production_inputs(
        session, source.in_(select(lineage.c.lot_id)), require_output_lot=not upstream
    )


def _list_production_inputs(
    session: Session,
    condition,
    *,
    require_output_lot: bool = False,
) -> Sequence[ProductionInput]:
    statement = (
        select(ProductionInput)
        .join(ProductionInput.production_run)
        .options(
            joinedload(ProductionInput.input_lot).joinedload(Lot.product),
            joinedload(ProductionInput.input_lot).selectinload(Lot.units),
            joinedload(ProductionInput.production_run)
            .joinedload(ProductionRun.output_lot)
            .joinedload(Lot.product),
            joinedload(ProductionInput.production_run)
            .joinedload(ProductionRun.output_lot)
            .selectinload(Lot.units),
            selectinload(ProductionInput.units).joinedload(ProductionInputUnit.unit),
        )
        .where(condition)
    )

    if require_output_lot:
        statement = statement.where(ProductionRun.output_lot_id.is_not(None))

    return session.scalars(statement).unique().all()


def list_deliveries(session: Session, lot_ids: set[UUID]) -> Sequence[OrderUnit]:
    return session.scalars(
        select(OrderUnit)
        .join(OrderUnit.unit)
        .where(Unit.lot_id.in_(lot_ids), OrderUnit.dispatched_at.is_not(None))
        .options(
            joinedload(OrderUnit.unit),
            joinedload(OrderUnit.order_item).joinedload(OrderItem.order),
        )
        .order_by(OrderUnit.dispatched_at, OrderUnit.id)
    ).all()
