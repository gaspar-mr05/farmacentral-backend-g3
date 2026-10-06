from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.db.locations import upsert_locations
from app.db.lots import upsert_lots
from app.db.production_runs import find_pending_runs_for_sku
from app.db.products import upsert_products
from app.db.units import UnitLocationChange, upsert_units
from app.models import CustodyEventType, Lot, LotOrigin, Product, ProductionRun, Unit
from app.schemas.inventory import InventoryData
from app.services.custody import events as custody_events
from app.services.inventory.collection import InventoryCollector
from app.services.inventory.normalization import normalize_inventory
from app.services.production.runs import link_production_run_to_existing_lot

INVENTORY_SYNC_LOCK_ID = 0x4641524D


@dataclass(frozen=True)
class EntityChanges:
    created: int = 0
    updated: int = 0


@dataclass(frozen=True)
class InventorySyncResult:
    products: EntityChanges
    locations: EntityChanges
    lots: EntityChanges
    units: EntityChanges


class InventorySyncService:
    """Coordinates collection, normalization, and atomic local persistence."""

    def __init__(self, client: FarmaCentralClient, session: Session) -> None:
        self._collector = InventoryCollector(client)
        self._session = session

    async def synchronize(self) -> InventorySyncResult:
        catalog, spaces, units, incomplete_groups = await self._collector.collect()
        recently_produced_at = datetime.now(UTC) - timedelta(minutes=10)
        production_skus = set(
            self._session.scalars(
                select(ProductionRun.expected_sku).where(
                    (ProductionRun.completed_at.is_(None))
                    | (ProductionRun.available_at >= recently_produced_at)
                )
            )
        )
        if production_skus:
            production_units = await self._collector.collect_units_for_skus(
                spaces, production_skus
            )
            units_by_id = {unit.external_id: unit for unit in units}
            units_by_id.update({unit.external_id: unit for unit in production_units})
            units = list(units_by_id.values())
        data = normalize_inventory(catalog, spaces, units)
        try:
            self._session.execute(
                select(func.pg_advisory_xact_lock(INVENTORY_SYNC_LOCK_ID))
            )
            result = sync_inventory(
                self._session,
                data,
                incomplete_groups=incomplete_groups,
            )
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise


def sync_inventory(
    session: Session,
    data: InventoryData,
    *,
    incomplete_groups: set[tuple[str, str]] | None = None,
) -> InventorySyncResult:
    products = upsert_products(session, data.products)
    locations = upsert_locations(session, data.locations)
    session.flush()

    lots = upsert_lots(session, data.lots, products.by_sku)
    session.flush()

    units = upsert_units(
        session,
        data.units,
        lots.by_external_id,
        locations.by_code,
        incomplete_groups=incomplete_groups,
    )
    _link_new_production_outputs(session, units.created_units)
    _record_custody_changes(session, units.location_changes)
    _delete_empty_unreported_lots(session)
    session.flush()

    return InventorySyncResult(
        products=EntityChanges(products.created, products.updated),
        locations=EntityChanges(locations.created, locations.updated),
        lots=EntityChanges(lots.created, lots.updated),
        units=EntityChanges(units.created, units.updated),
    )


def _record_custody_changes(
    session: Session,
    changes: tuple[UnitLocationChange, ...],
) -> None:
    for change in changes:
        if change.is_received:
            lot = change.unit.lot  # lazy-loaded si no está ya en la sesión
            event_type = (
                CustodyEventType.PRODUCED
                if lot.origin == LotOrigin.OWN_PRODUCTION
                else CustodyEventType.RECEIVED
            )
        else:
            event_type = CustodyEventType.MOVED

        custody_events.log_custody_event(
            session,
            unit_id=change.unit.id,
            event_type=event_type,
            from_location_id=change.from_location_id,
            to_location_id=change.to_location_id,
        )


def _link_new_production_outputs(
    session: Session, created_units: tuple[Unit, ...]
) -> None:
    created_skus = {unit.lot.product.sku for unit in created_units}
    pending_skus = set(
        session.scalars(
            select(ProductionRun.expected_sku).where(
                ProductionRun.completed_at.is_(None)
            )
        )
    )

    for sku in created_skus | pending_skus:
        runs = find_pending_runs_for_sku(session, sku=sku)
        if not runs:
            continue
        sku_units = list(
            session.scalars(
                select(Unit)
                .join(Lot, Unit.lot_id == Lot.id)
                .join(Product, Lot.product_id == Product.id)
                .where(
                    Product.sku == sku,
                    Unit.created_at >= runs[0].requested_at,
                    ~Lot.produced_by.has(),
                )
                .order_by(Unit.created_at, Unit.id)
            )
        )
        offset = 0
        for run in runs:
            selected = sku_units[offset : offset + run.expected_quantity]
            if len(selected) < run.expected_quantity:
                break

            source_lots = {unit.lot for unit in selected}
            if len(source_lots) == 1:
                output_lot = source_lots.pop()
                output_lot.origin = LotOrigin.OWN_PRODUCTION
            else:
                output_lot = Lot(
                    external_lot_id=f"production:{run.id}",
                    product_id=selected[0].lot.product_id,
                    expires_at=min(unit.effective_expires_at for unit in selected),
                    origin=LotOrigin.OWN_PRODUCTION,
                )
                session.add(output_lot)
                session.flush()
                for unit in selected:
                    unit.lot = output_lot

            session.flush()
            link_production_run_to_existing_lot(
                session,
                run=run,
                output_lot_id=output_lot.id,
                completed_at=datetime.now(UTC),
            )
            offset += run.expected_quantity


def _delete_empty_unreported_lots(session: Session) -> None:
    candidates = session.scalars(
        select(Lot).where(Lot.external_lot_id.startswith("unreported:"))
    )
    for lot in candidates:
        unit_count = session.scalar(
            select(func.count()).select_from(Unit).where(Unit.lot_id == lot.id)
        )
        if unit_count == 0:
            session.delete(lot)
