import logging
from collections import defaultdict

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, joinedload

from app.clients.farma_central import FarmaCentralInvalidResponseError
from app.models import (
    Lot,
    LotIdentityRepair,
    ProductionInput,
    ProductionRun,
    Recall,
    RecallLot,
    Unit,
)
from app.schemas.units import UnitData

logger = logging.getLogger(__name__)
PROVISIONAL_PREFIXES = ("unreported:", "production:")


def recover_reported_lots(
    session: Session,
    records: tuple[UnitData, ...],
    skus_by_batch: dict[str, str],
) -> int:
    """Recover only identities explicitly supplied for these exact unit IDs.

    Must run inside the inventory synchronization transaction. Renaming an entire
    provisional lot keeps its UUID and lineage. Merging lots also updates input
    references; audited recall snapshots are never rewritten implicitly.
    """
    reported = {
        record.external_unit_id: record
        for record in records
        if not record.lot_external_id.startswith(PROVISIONAL_PREFIXES)
    }
    if not reported:
        return 0
    candidates = session.scalars(
        select(Unit)
        .join(Lot, Unit.lot_id == Lot.id)
        .where(
            Unit.external_unit_id.in_(reported),
            (Lot.external_lot_id.startswith("unreported:"))
            | (Lot.external_lot_id.startswith("production:")),
        )
        .options(joinedload(Unit.lot).joinedload(Lot.product))
        .order_by(Unit.id)
        .with_for_update(of=Unit)
        .execution_options(populate_existing=True)
    ).all()
    if not candidates:
        return 0
    sources = {unit.lot_id: unit.lot for unit in candidates}
    source_units = session.scalars(
        select(Unit).where(Unit.lot_id.in_(sources)).order_by(Unit.id).with_for_update()
    ).all()
    grouped = defaultdict(list)
    for unit in source_units:
        grouped[unit.lot_id].append(unit)
    batch_groups = defaultdict(list)
    for lot_id, lot in sources.items():
        members = grouped[lot_id]
        if any(unit.external_unit_id not in reported for unit in members):
            raise FarmaCentralInvalidResponseError(
                "Cannot recover a partial production lot; "
                "complete unit-to-batch evidence is required"
            )
        batches = {reported[unit.external_unit_id].lot_external_id for unit in members}
        if len(batches) != 1:
            raise FarmaCentralInvalidResponseError(
                "A provisional production lot spans multiple official batches"
            )
        batch = batches.pop()
        if skus_by_batch.get(batch) != lot.product.sku:
            raise FarmaCentralInvalidResponseError(
                "Official unit SKU differs from local history"
            )
        batch_groups[batch].append(lot)

    plans = []
    for batch, lots in batch_groups.items():
        target = session.scalar(
            select(Lot).where(Lot.external_lot_id == batch).with_for_update()
        )
        produced = [lot for lot in lots if lot.produced_by is not None]
        producers = produced + (
            [target] if target is not None and target.produced_by else []
        )
        if len(producers) > 1:
            raise FarmaCentralInvalidResponseError(
                "Cannot merge different production runs"
            )
        if target is None:
            target = produced[0] if produced else lots[0]
        for lot in lots:
            if lot.product_id != target.product_id:
                raise FarmaCentralInvalidResponseError(
                    "Official batch has conflicting SKUs"
                )
            if lot.id != target.id and (
                session.scalar(select(RecallLot.id).where(RecallLot.lot_id == lot.id))
                or session.scalar(
                    select(Recall.id).where(Recall.origin_lot_id == lot.id)
                )
            ):
                raise FarmaCentralInvalidResponseError(
                    "Cannot merge a lot already referenced by a recall snapshot"
                )
            plans.append((lot, target, batch))

    for lot, target, batch in plans:
        previous = lot.external_lot_id
        for unit in grouped[lot.id]:
            session.add(
                LotIdentityRepair(
                    unit_id=unit.id, previous_batch=previous, official_batch=batch
                )
            )
        if lot.id == target.id:
            lot.external_lot_id = batch
            continue
        if lot.produced_by is not None:
            target.origin = lot.origin
            session.execute(
                update(ProductionRun)
                .where(ProductionRun.output_lot_id == lot.id)
                .values(output_lot_id=target.id)
            )
        session.execute(
            update(ProductionInput)
            .where(ProductionInput.input_lot_id == lot.id)
            .values(input_lot_id=target.id)
        )
        session.execute(
            update(Unit).where(Unit.lot_id == lot.id).values(lot_id=target.id)
        )
        session.execute(delete(Lot).where(Lot.id == lot.id))
    session.flush()
    session.expire_all()
    logger.info(
        "Recovered official lot identities: units=%s lots=%s",
        len(source_units),
        len(batch_groups),
    )
    return len(source_units)
