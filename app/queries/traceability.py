from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
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


def get_lot_with_units(session: Session, lot_id: UUID) -> Lot | None:
    statement = (
        select(Lot)
        .options(
            joinedload(Lot.product),
            selectinload(Lot.units).joinedload(Unit.current_location),
        )
        .where(Lot.id == lot_id)
    )
    return session.scalar(statement)


def list_upstream_inputs(
    session: Session,
    output_lot_ids: set[UUID],
) -> Sequence[ProductionInput]:
    return _list_production_inputs(
        session,
        ProductionRun.output_lot_id.in_(output_lot_ids),
    )


def list_downstream_inputs(
    session: Session,
    input_lot_ids: set[UUID],
) -> Sequence[ProductionInput]:
    return _list_production_inputs(
        session,
        ProductionInput.input_lot_id.in_(input_lot_ids),
        require_output_lot=True,
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
            joinedload(ProductionInput.production_run)
            .joinedload(ProductionRun.output_lot)
            .joinedload(Lot.product),
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
