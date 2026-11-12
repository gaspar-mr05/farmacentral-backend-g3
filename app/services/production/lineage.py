"""Checks used when preparing stock for a recall validation."""

from uuid import UUID

from app.models import Lot, ProductCategory


def has_official_lineage(
    lot: Lot, path: frozenset[UUID] = frozenset(), *, allow_external=False
) -> bool:
    if (
        lot.id in path
        or not lot.external_lot_id.strip()
        or lot.external_lot_id.startswith(("unreported:", "production:"))
    ):
        return False
    run = lot.produced_by
    if run is None:
        return allow_external or lot.product.category == ProductCategory.INSUMO
    if run.completed_at is None or not run.inputs:
        return False
    return all(
        item.quantity_consumed > 0
        and len(item.units) == item.quantity_consumed
        and has_official_lineage(
            item.input_lot, path | {lot.id}, allow_external=allow_external
        )
        for item in run.inputs
    )
