from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Lot,
    OrderUnit,
    ProductionInput,
    ProductionInputUnit,
    ProductionRun,
    Unit,
)

STOCK_STATUSES = ("available", "reserved", "recall_pending", "quarantined", "expired")


def get_origin_lot(session: Session, batch: str) -> Lot | None:
    return session.scalar(
        select(Lot).where(Lot.external_lot_id == batch).options(joinedload(Lot.product))
    )


def affected_lineage(session: Session, origin_lot_id: UUID):
    affected = (
        select(Lot.id.label("lot_id"))
        .where(Lot.id == origin_lot_id)
        .cte(
            "affected_lots",
            recursive=True,
        )
    )
    # UNION (rather than UNION ALL) visits each lot once, including in a cycle.
    affected = affected.union(
        select(ProductionRun.output_lot_id)
        .join(ProductionInput, ProductionInput.production_run_id == ProductionRun.id)
        .join(affected, affected.c.lot_id == ProductionInput.input_lot_id)
        .where(
            ProductionRun.output_lot_id.is_not(None),
            ProductionInput.quantity_consumed > 0,
        )
    )
    return session.execute(
        select(
            affected.c.lot_id,
            ProductionRun.output_lot_id.label("child_lot_id"),
            ProductionRun.id.label("production_run_id"),
        )
        .select_from(affected)
        .outerjoin(
            ProductionInput,
            (ProductionInput.input_lot_id == affected.c.lot_id)
            & (ProductionInput.quantity_consumed > 0),
        )
        .outerjoin(ProductionRun, ProductionRun.id == ProductionInput.production_run_id)
        .distinct()
    ).all()


def affected_lots(session: Session, lot_ids: set[UUID]) -> list[Lot]:
    return list(
        session.scalars(
            select(Lot)
            .where(Lot.id.in_(lot_ids))
            .options(joinedload(Lot.product))
            .order_by(Lot.external_lot_id)
        )
    )


def stock_units(session: Session, lot_ids: set[UUID]) -> list[Unit]:
    return list(
        session.scalars(
            select(Unit)
            .where(
                Unit.lot_id.in_(lot_ids),
                Unit.status.in_(STOCK_STATUSES),
                ~Unit.order_assignment.has(OrderUnit.dispatched_at.is_not(None)),
                ~exists().where(ProductionInputUnit.unit_id == Unit.id),
            )
            .options(joinedload(Unit.current_location))
            .order_by(Unit.external_unit_id)
            .with_for_update(of=Unit)
        )
    )
