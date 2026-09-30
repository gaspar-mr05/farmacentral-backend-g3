# app/services/inventory/sync.py
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.db.locations import upsert_locations
from app.db.lots import upsert_lots
from app.db.production_runs import find_pending_run_for_sku
from app.db.products import upsert_products
from app.db.units import UnitLocationChange, upsert_units
from app.models import CustodyEventType, Lot, LotOrigin
from app.schemas.inventory import InventoryData
from app.services.custody import events as custody_events
from app.services.inventory.collection import InventoryCollector
from app.services.inventory.normalization import normalize_inventory
from app.services.production.runs import finish_production_run
from app.services.production.runs import link_production_run_to_existing_lot


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
        catalog, spaces, units = await self._collector.collect()
        data = normalize_inventory(catalog, spaces, units)
        try:
            result = sync_inventory(self._session, data)
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise


def sync_inventory(session: Session, data: InventoryData) -> InventorySyncResult:
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
    )
    _record_custody_changes(session, units.location_changes)
    session.flush()

    if lots.created_lots:
        _handle_new_output_lots(session, lots.created_lots)
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


def _handle_new_output_lots(session: Session, new_lots: list[Lot]) -> None:
    for lot in new_lots:
        if lot.origin != LotOrigin.OWN_PRODUCTION:
            continue
        run = find_pending_run_for_sku(session, sku=lot.product.sku)
        if run is not None:
            link_production_run_to_existing_lot(
                session, run=run, output_lot_id=lot.id, completed_at=datetime.now(timezone.utc)
            )