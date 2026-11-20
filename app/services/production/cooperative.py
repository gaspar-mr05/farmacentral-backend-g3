"""One manufacturing operation per turn; availability waits happen outside the lock."""

from datetime import UTC, datetime
from decimal import Decimal
from math import ceil
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import and_, or_, select

from app.models import (
    Location,
    Lot,
    MarketOffer,
    MarketOfferReservation,
    MarketOfferSide,
    Product,
    ProductionRun,
    ReplenishmentState,
    Unit,
)
from app.queries.market import accounted_deliveries
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
    def __init__(self, sku):
        self.sku = sku
        super().__init__(f"Supply policy must resolve {sku}")


class CooperativeKitProduction(KitProductionService):
    def __init__(
        self,
        client,
        session,
        state,
        catalog,
        spaces,
        *,
        group_id,
        inventory_fresh=False,
    ):
        super().__init__(
            client,
            session,
            raw_material_source="supply",
            require_official_batches=True,
            allow_external_batches=True,
        )
        self.protected_stock = {}
        self.sourcing_skus = set()
        self._group_id = group_id
        self._inventory_fresh = inventory_fresh
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
        if self._inventory_fresh:
            # The replenishment poll already took a complete snapshot before
            # planning this turn. Movements performed since then update the
            # local ledger transactionally, so repeating the remote scan here
            # only delays manufacturing by several minutes under API pacing.
            self._inventory_fresh = False
            return
        await InventorySyncService(self._client, self._session).synchronize(
            tolerate_changes=True
        )

    async def _provision_raw_material(self, product, missing):
        raise SupplyNeeded(product.sku)

    async def _ensure_available(self, sku, quantity, *, dependency_path):
        if len(self._available_units(sku)) >= quantity:
            return
        if sku in self.sourcing_skus:
            # A planned purchase must not silently turn into a different recipe.
            raise SupplyNeeded(sku)
        product = self._catalog.get(sku)
        if product is None:
            raise ProductionCatalogError(f"Product {sku} is missing from catalog")
        if product.production.at == "packaging":
            await self._produce_quantity(
                product, quantity, dependency_path=dependency_path
            )
        else:
            raise SupplyNeeded(sku)

    async def _clear_packaging(self):
        await self.put_away(include_packaging=True, receiving_skus=())

    async def put_away(self, *, include_packaging=False, receiving_skus=None):
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
        eligible_source = Location.code.in_(sources)
        if receiving_skus is not None:
            # Components can be staged directly from reception. Move finished
            # kits into sellable storage and clear packaging, without relocating
            # hundreds of unrelated inputs before a manufacturing turn.
            eligible_source &= or_(
                Location.code == self._packaging_code,
                Unit.lot.has(Lot.product.has(Product.sku.in_(receiving_skus))),
            )
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
                eligible_source,
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

    def ready_final_quantity(self, quantity):
        product = self._catalog[self.state.sku]
        available = len(self._available_units(product.sku))
        ready = self.ready_final_capacity(quantity)
        return ready if available < ready < quantity else None

    def ready_final_capacity(self, quantity):
        """Return final stock that can exist after one ready-only production."""
        product = self._catalog[self.state.sku]
        available = len(self._available_units(product.sku))
        missing = quantity - available
        if missing <= 0 or not product.sellable or not product.components:
            return available
        ready = min(
            missing,
            self._maximum_chunk_size(product),
            *(
                len(self._available_units(c.sku)) // c.required_per_unit
                for c in product.components
            ),
        )
        ready -= ready % product.production.batch
        return available + ready

    async def advance(self, sku, quantity, *, ready_only=False):
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
                self._catalog[sku], quantity, dependency_path=(), ready_only=ready_only
            )

    async def _produce_quantity(
        self, product, quantity, *, dependency_path, ready_only=False
    ):
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
                MarketOffer.id.not_in(accounted_deliveries(self.state.period_id)),
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
        if ready_only and any(
            len(self._available_units(c.sku)) < c.required_per_unit * chunk
            for c in product.components
        ):
            raise ReplenishmentDeferred(
                "Partial kit stock changed; revalidate next turn"
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
