from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lot, Product
from app.schemas.lots import LotData


@dataclass(frozen=True)
class LotUpsertResult:
    created: int
    updated: int
    by_external_id: dict[str, Lot]
    created_lots: tuple[Lot, ...] = field(default_factory=tuple)


def upsert_lots(
    session: Session,
    records: Iterable[LotData],
    products_by_sku: dict[str, Product],
) -> LotUpsertResult:
    lots = {lot.external_lot_id: lot for lot in session.scalars(select(Lot))}
    created = updated = 0
    created_lots: list[Lot] = []

    for record in records:
        product = products_by_sku[record.product_sku]
        lot = lots.get(record.external_lot_id)
        if lot is None:
            lot = Lot(
                external_lot_id=record.external_lot_id,
                product_id=product.id,
                expires_at=record.expires_at,
                origin=record.origin,
            )
            session.add(lot)
            lots[record.external_lot_id] = lot
            created_lots.append(lot)
            created += 1
            continue

        changed = (
            lot.product_id != product.id
            or lot.expires_at != record.expires_at
            or lot.origin != record.origin
        )
        lot.product_id = product.id
        lot.expires_at = record.expires_at
        lot.origin = record.origin
        updated += int(changed)

    return LotUpsertResult(created, updated, lots, tuple(created_lots))
