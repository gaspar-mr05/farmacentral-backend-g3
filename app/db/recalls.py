from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    InboundEvent,
    InboundEventSource,
    Recall,
    RecallDelivery,
    RecallLot,
    RecallOrder,
    RecallUnit,
    Unit,
)
from app.schemas.recall_investigation import RecallInvestigation


class RecallSnapshotConflictError(Exception):
    pass


def lock_recall_event(session: Session, recall_id: str) -> InboundEvent | None:
    return session.scalar(
        select(InboundEvent)
        .where(
            InboundEvent.source == InboundEventSource.RECALL,
            InboundEvent.external_event_id == recall_id,
        )
        .with_for_update()
    )


def lock_recall(session: Session, recall_id: str) -> Recall | None:
    return session.scalar(
        select(Recall)
        .where(Recall.recall_id == recall_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def store_investigation(
    session: Session,
    recall: Recall,
    findings: RecallInvestigation,
    units: list[Unit],
) -> dict[UUID, RecallUnit]:
    lots = {
        record.lot_id: record
        for record in session.scalars(
            select(RecallLot).where(RecallLot.recall_id == recall.id)
        )
    }
    if lots.keys() - {lot.lot_id for lot in findings.lots}:
        raise RecallSnapshotConflictError(
            "The stored recall contains lots outside its lineage"
        )
    for lot in findings.lots:
        parent = lots[lot.parent_lot_id].id if lot.parent_lot_id is not None else None
        if lot.lot_id not in lots:
            lots[lot.lot_id] = RecallLot(
                id=uuid4(),
                recall_id=recall.id,
                lot_id=lot.lot_id,
                generation=lot.generation,
                parent_id=parent,
            )
            session.add(lots[lot.lot_id])
        else:
            lots[lot.lot_id].generation = lot.generation
            lots[lot.lot_id].parent_id = parent
    session.flush()

    stock = {
        record.unit_id: record
        for record in session.scalars(
            select(RecallUnit)
            .where(RecallUnit.recall_id == recall.id)
            .options(joinedload(RecallUnit.original_location))
        )
    }
    for unit in units:
        if unit.id not in stock:
            stock[unit.id] = RecallUnit(
                recall_id=recall.id,
                lot_id=unit.lot_id,
                unit_id=unit.id,
                original_location=unit.current_location,
            )
            session.add(stock[unit.id])

    orders = {
        record.order_id: record
        for record in session.scalars(
            select(RecallOrder).where(RecallOrder.recall_id == recall.id)
        )
    }
    assignments = set(
        session.scalars(
            select(RecallDelivery.order_unit_id).where(
                RecallDelivery.recall_id == recall.id,
            )
        )
    )
    for order in findings.orders:
        if order.order_id not in orders:
            orders[order.order_id] = RecallOrder(
                id=uuid4(),
                recall_id=recall.id,
                order_id=order.order_id,
                customer_reference=order.customer_reference,
            )
            session.add(orders[order.order_id])
    session.flush()
    for order in findings.orders:
        for delivery in order.deliveries:
            if delivery.order_unit_id not in assignments:
                session.add(
                    RecallDelivery(
                        recall_id=recall.id,
                        recall_order_id=orders[order.order_id].id,
                        lot_id=delivery.lot_id,
                        unit_id=delivery.unit_id,
                        order_unit_id=delivery.order_unit_id,
                        delivered_at=delivery.dispatched_at,
                    )
                )
    session.flush()
    return stock
