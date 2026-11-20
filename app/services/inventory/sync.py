import json
import logging
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.db.locations import upsert_locations
from app.db.lots import upsert_lots
from app.db.production_runs import find_pending_runs_for_sku
from app.db.products import upsert_products
from app.db.units import UnitLocationChange, upsert_units
from app.models import CustodyEventType, Lot, LotOrigin, Product, ProductionRun, Unit
from app.queries.recalls import block_recalled_stock
from app.schemas.inventory import InventoryData
from app.services.custody import events as custody_events
from app.services.inventory.collection import InventoryCollector
from app.services.inventory.lot_identity import recover_reported_lots
from app.services.inventory.normalization import normalize_inventory
from app.services.production.runs import link_production_run_to_existing_lot

INVENTORY_SYNC_LOCK_ID = 0x4641524D
logger = logging.getLogger(__name__)


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
    inventory_complete: bool = False
    created_unit_ids: tuple[str, ...] = ()
    missing_unit_ids: tuple[str, ...] = ()
    complete_skus: frozenset[str] = frozenset()
    incomplete_groups: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class InventoryObservation:
    started_at: datetime
    completed_at: datetime
    result: InventorySyncResult
    reused: bool = False


class InventorySyncService:
    """Coordinates collection, normalization, and atomic local persistence."""

    def __init__(self, client: FarmaCentralClient, session: Session) -> None:
        self._client = client
        self._collector = InventoryCollector(client)
        self._session = session

    async def synchronize(self, *, tolerate_changes=False) -> InventorySyncResult:
        snapshot_started_at = datetime.now(UTC)
        collector = (
            InventoryCollector(self._client, tolerate_changes=True)
            if tolerate_changes
            else self._collector
        )
        catalog, spaces, units, incomplete_groups = await collector.collect()
        recently_produced_at = datetime.now(UTC) - timedelta(minutes=10)
        production_skus = set(
            self._session.scalars(
                select(ProductionRun.expected_sku).where(
                    (ProductionRun.completed_at.is_(None))
                    | (ProductionRun.available_at >= recently_produced_at)
                )
            )
        )
        if production_skus and not tolerate_changes:
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
                uncertain_missing=tolerate_changes,
                snapshot_started_at=snapshot_started_at,
            )
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise

    async def observe_recent(
        self,
        *,
        path: Path,
        max_age_seconds: float,
        tolerate_changes: bool = False,
    ) -> InventoryObservation:
        """Reuse a recent complete scan while retaining its actual start time."""
        now = datetime.now(UTC)
        if max_age_seconds > 0:
            cached = _read_observation(path)
            if (
                cached is not None
                and cached.result.inventory_complete
                and all(
                    value.utcoffset() is not None
                    for value in (cached.started_at, cached.completed_at)
                )
            ):
                age = (now - cached.completed_at).total_seconds()
                if 0 <= age <= max_age_seconds:
                    return cached

        started_at = now
        result = await self.synchronize(tolerate_changes=tolerate_changes)
        observation = InventoryObservation(
            started_at=started_at,
            completed_at=datetime.now(UTC),
            result=result,
        )
        if max_age_seconds > 0 and result.inventory_complete:
            try:
                _write_observation(path, observation)
            except OSError as exc:
                logger.warning(
                    "Inventory observation cache unavailable: error_type=%s",
                    type(exc).__name__,
                )
        return observation


def _read_observation(path: Path) -> InventoryObservation | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            payload = json.load(handle)
        result = payload["result"]
        return InventoryObservation(
            started_at=datetime.fromisoformat(payload["started_at"]),
            completed_at=datetime.fromisoformat(payload["completed_at"]),
            result=InventorySyncResult(
                products=EntityChanges(**result["products"]),
                locations=EntityChanges(**result["locations"]),
                lots=EntityChanges(**result["lots"]),
                units=EntityChanges(**result["units"]),
                inventory_complete=result["inventory_complete"],
                created_unit_ids=tuple(result["created_unit_ids"]),
                missing_unit_ids=tuple(result["missing_unit_ids"]),
                complete_skus=frozenset(result["complete_skus"]),
                incomplete_groups=frozenset(
                    tuple(group) for group in result["incomplete_groups"]
                ),
            ),
            reused=True,
        )
    except (
        FileNotFoundError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def _write_observation(path: Path, observation: InventoryObservation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = asdict(observation.result)
    result["complete_skus"] = sorted(observation.result.complete_skus)
    result["incomplete_groups"] = sorted(
        [list(group) for group in observation.result.incomplete_groups]
    )
    payload = {
        "started_at": observation.started_at.isoformat(),
        "completed_at": observation.completed_at.isoformat(),
        "result": result,
    }
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def sync_inventory(
    session: Session,
    data: InventoryData,
    *,
    incomplete_groups: set[tuple[str, str]] | None = None,
    uncertain_missing: bool = False,
    snapshot_started_at: datetime | None = None,
) -> InventorySyncResult:
    products = upsert_products(session, data.products)
    locations = upsert_locations(session, data.locations)
    session.flush()

    recover_reported_lots(
        session,
        data.units,
        {lot.external_lot_id: lot.product_sku for lot in data.lots},
    )
    lots = upsert_lots(session, data.lots, products.by_sku)
    session.flush()

    units = upsert_units(
        session,
        data.units,
        lots.by_external_id,
        locations.by_code,
        incomplete_groups=incomplete_groups,
        uncertain_missing=uncertain_missing,
        snapshot_started_at=snapshot_started_at,
    )
    _link_new_production_outputs(session, units.created_units)
    session.flush()
    block_recalled_stock(session)
    _record_custody_changes(session, units.location_changes)
    _delete_empty_unreported_lots(session)
    session.flush()

    incomplete_skus = {sku for _, sku in (incomplete_groups or ())}
    return InventorySyncResult(
        products=EntityChanges(products.created, products.updated),
        locations=EntityChanges(locations.created, locations.updated),
        lots=EntityChanges(lots.created, lots.updated),
        units=EntityChanges(units.created, units.updated),
        inventory_complete=bool(
            data.products and data.locations and not incomplete_groups
        ),
        created_unit_ids=tuple(unit.external_unit_id for unit in units.created_units),
        missing_unit_ids=tuple(unit.external_unit_id for unit in units.missing_units),
        complete_skus=frozenset(
            product.sku
            for product in data.products
            if data.locations and product.sku not in incomplete_skus
        ),
        incomplete_groups=frozenset(incomplete_groups or ()),
    )


def _record_custody_changes(
    session: Session,
    changes: tuple[UnitLocationChange, ...],
) -> None:
    for change in changes:
        if change.is_received:
            lot = change.unit.lot
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
    created_unit_ids = {unit.id for unit in created_units}
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
                    or_(
                        Unit.id.in_(created_unit_ids),
                        Unit.created_at >= runs[0].requested_at,
                    ),
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
