from uuid import UUID

from sqlalchemy import Select, exists, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Lot,
    OrderUnit,
    ProductionInput,
    ProductionInputUnit,
    ProductionRun,
    Recall,
    RecallDelivery,
    RecallLot,
    RecallStatus,
    RecallUnit,
    Unit,
)

STOCK_STATUSES = ("available", "reserved", "recall_pending", "quarantined", "expired")


def blocked_lots():
    """Include outputs linked after an investigation, even after its deadline."""
    affected = (
        select(RecallLot.lot_id)
        .join(Recall, Recall.id == RecallLot.recall_id)
        .where(
            Recall.status.in_(
                [
                    RecallStatus.RESPONDING,
                    RecallStatus.FAILED,
                    RecallStatus.COMPLETED,
                ]
            )
        )
        .cte("blocked_lots", recursive=True)
    )
    return affected.union(
        select(ProductionRun.output_lot_id)
        .join(ProductionInput, ProductionInput.production_run_id == ProductionRun.id)
        .join(affected, affected.c.lot_id == ProductionInput.input_lot_id)
        .where(
            ProductionRun.output_lot_id.is_not(None),
            ProductionInput.quantity_consumed > 0,
        )
    )


def block_recalled_stock(session: Session) -> None:
    affected = blocked_lots()
    for unit in stock_units(session, select(affected.c.lot_id)):
        if unit.status != "quarantined":
            unit.status = "recall_pending"


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


def stock_units(session: Session, lot_ids: set[UUID] | Select) -> list[Unit]:
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


def findings_changed(session: Session, recall: Recall) -> bool:
    affected = {row.lot_id for row in affected_lineage(session, recall.origin_lot_id)}
    recorded = set(
        session.scalars(
            select(RecallLot.lot_id).where(RecallLot.recall_id == recall.id)
        )
    )
    quarantined = set(
        session.scalars(
            select(RecallUnit.unit_id).where(
                RecallUnit.recall_id == recall.id,
                RecallUnit.quarantined_at.is_not(None),
            )
        )
    )
    stock_changed = any(
        unit.id not in quarantined for unit in stock_units(session, affected)
    )
    deliveries = set(
        session.scalars(
            select(OrderUnit.id)
            .join(Unit)
            .where(
                Unit.lot_id.in_(affected),
                OrderUnit.dispatched_at.is_not(None),
            )
        )
    )
    recorded_deliveries = set(
        session.scalars(
            select(RecallDelivery.order_unit_id).where(
                RecallDelivery.recall_id == recall.id
            )
        )
    )
    return affected != recorded or stock_changed or deliveries != recorded_deliveries
