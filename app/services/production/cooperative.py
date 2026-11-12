"""One manufacturing operation per turn; availability waits happen outside the lock."""

from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import and_, or_, select

from app.models import (
    Location,
    MarketOffer,
    MarketOfferReservation,
    MarketOfferSide,
    ProductionRun,
    ReplenishmentState,
    Unit,
)
from app.queries.recalls import blocked_lots
from app.schemas.farma_central import FarmaCentralSpace
from app.services.inventory.movements import ProductMovementService
from app.services.inventory.sync import InventorySyncService
from app.services.production.kits import KitProductionService, ProductionCatalogError
from app.services.production.locking import production_lock
from app.services.production.orchestration import produce


class ReplenishmentDeferred(Exception):
    pass


class SupplyNeeded(ReplenishmentDeferred):
    pass


class CooperativeKitProduction(KitProductionService):
    def __init__(self, client, session, state, catalog, spaces, *, group_id):
        super().__init__(
            client,
            session,
            raw_material_source="supply",
            require_official_batches=True,
            allow_external_batches=True,
        )
        self.protected_stock = {}
        self._group_id = group_id
        self.state = state
        self._catalog = catalog
        self.spaces = spaces
        packaging = [s for s in spaces if s.packaging]
        if len(packaging) != 1 or not packaging[0].total_space:
            raise ProductionCatalogError(
                "Exactly one packaging area with known capacity is required"
            )
        self._packaging_code = packaging[0].external_id
        self._packaging_capacity = packaging[0].total_space

    def _available_units(self, sku):
        units = super()._available_units(sku)
        if sku == self.state.sku and self.state.goal:
            deadline = datetime.fromisoformat(self.state.goal["deadline"])
            units = [u for u in units if u.effective_expires_at > deadline]
        keep = self.protected_stock.get(sku, 0)
        return units[keep:] if sku != self.state.sku else units

    async def _synchronize(self):
        await InventorySyncService(self._client, self._session).synchronize(
            tolerate_changes=True
        )

    async def _provision_raw_material(self, product, missing):
        raise SupplyNeeded(f"Supply policy must resolve {product.sku}")

    async def _ensure_available(self, sku, quantity, *, dependency_path):
        if len(self._available_units(sku)) >= quantity:
            return
        product = self._catalog.get(sku)
        if product is None:
            raise ProductionCatalogError(f"Product {sku} is missing from catalog")
        if product.production.at == "packaging":
            await self._produce_quantity(
                product, quantity, dependency_path=dependency_path
            )
        else:
            raise SupplyNeeded(f"Supply policy must resolve {sku}")

    async def _clear_packaging(self):
        await self.put_away(include_packaging=True)

    async def put_away(self, *, include_packaging=False):
        # Remote capacities are refreshed; confirmed moves update the local ledger.
        spaces = TypeAdapter(list[FarmaCentralSpace]).validate_python(
            await self._client.get_spaces()
        )
        sources = {
            s.external_id
            for s in spaces
            if s.check_in or (include_packaging and s.packaging)
        }
        destinations = [
            s
            for s in spaces
            if not (s.check_in or s.check_out or s.packaging or s.quarantine)
            and s.total_space is not None
            and s.used_space is not None
        ]
        destinations.sort(key=lambda s: (s.buffer, s.cold, s.external_id))
        free = {
            s.external_id: max(0, s.total_space - s.used_space) for s in destinations
        }
        units = self._session.scalars(
            select(Unit)
            .join(Location)
            .where(
                Unit.status == "available",
                ~Unit.order_assignment.has(),
                ~Unit.market_reservations.any(
                    MarketOfferReservation.released_at.is_(None)
                ),
                Unit.lot_id.not_in(select(blocked_lots().c.lot_id)),
                Location.code.in_(sources),
                Unit.effective_expires_at > datetime.now(UTC),
            )
            .order_by(Unit.effective_expires_at, Unit.id)
            .limit(201)
        ).all()
        for unit in units[:200]:
            target = next(
                (
                    s
                    for s in destinations
                    if free[s.external_id] > 0
                    and (not unit.lot.product.requires_refrigeration or s.cold)
                ),
                None,
            )
            if target is None:
                raise ReplenishmentDeferred(
                    "No safe storage capacity for received/produced stock"
                )
            await ProductMovementService(self._client, self._session).move(
                product_id=unit.external_unit_id, destination_store=target.external_id
            )
            free[target.external_id] -= 1
        if len(units) > 200:
            raise ReplenishmentDeferred(
                "Storage movement limit reached; resume next turn"
            )

    def _unknown_production(self):
        for state in self._session.scalars(
            select(ReplenishmentState).where(
                ReplenishmentState.pending_operation.is_not(None)
            )
        ):
            op = state.pending_operation or {}
            if op.get("kind") == "produce" and op.get("status") in {
                "in_flight",
                "uncertain",
            }:
                return True
        return False

    async def advance(self, sku, quantity):
        with production_lock(self._session):
            await self._synchronize()
            if self._unknown_production() or self._session.scalar(
                select(ProductionRun.id)
                .where(ProductionRun.completed_at.is_(None))
                .limit(1)
            ):
                raise ReplenishmentDeferred(
                    "Shared packaging has production pending recovery/availability"
                )
            await self._clear_packaging()
            await self._produce_quantity(
                self._catalog[sku], quantity, dependency_path=()
            )

    async def _produce_quantity(self, product, quantity, *, dependency_path):
        if product.sku in dependency_path or not product.components:
            raise ProductionCatalogError("Missing or cyclic production recipe")
        available = len(self._available_units(product.sku))
        if available >= quantity:
            return
        chunk = min(
            ceil((quantity - available) / product.production.batch)
            * product.production.batch,
            self._maximum_chunk_size(product),
        )
        # Do not mix incoming purchased outputs with an own production of the same SKU.
        incoming = self._session.scalar(
            select(MarketOffer.id)
            .where(
                MarketOffer.period_id == self.state.period_id,
                MarketOffer.sku == product.sku,
                MarketOffer.reconciled_at.is_(None),
                MarketOffer.status.not_in(["cancelled", "expired", "failed"]),
                or_(
                    and_(
                        MarketOffer.side == MarketOfferSide.BUY,
                        MarketOffer.publisher_group_id == self._group_id,
                    ),
                    and_(
                        MarketOffer.side == MarketOfferSide.SELL,
                        MarketOffer.publisher_group_id != self._group_id,
                    ),
                ),
            )
            .limit(1)
        )
        if incoming:
            raise ReplenishmentDeferred(
                "Reconcile the incoming purchase before manufacturing this SKU"
            )
        for component in product.components:
            await self._ensure_available(
                component.sku,
                component.required_per_unit * chunk,
                dependency_path=(*dependency_path, product.sku),
            )
        await self._clear_packaging()
        for component in product.components:
            await self._stage_component(
                component.sku, component.required_per_unit * chunk
            )
        key = uuid4()
        self.state.pending_operation = {
            "kind": "produce",
            "sku": product.sku,
            "quantity": chunk,
            "key": str(key),
            "status": "in_flight",
            "cost": str((product.cost or Decimal(0)) * chunk),
        }
        self._session.commit()
        run, response = await produce(
            self._session,
            client=self._client,
            sku=product.sku,
            quantity=chunk,
            operation_key=key,
        )
        self.state.pending_operation = self.state.pending_operation | {
            "status": "confirmed",
            "run_id": str(run.id),
            "available_at": response.available_at.isoformat(),
        }
        self._session.commit()
        # No sleep and no second recipe/production POST in this turn.
        raise ReplenishmentDeferred(
            "Production confirmed; revisit after its availability time"
        )
