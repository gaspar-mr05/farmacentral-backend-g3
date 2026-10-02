import asyncio
import logging
from datetime import UTC, datetime
from math import ceil
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.db.production_runs import find_pending_runs_for_sku
from app.models import Location, Lot, Product, ProductionRun, Unit
from app.schemas.farma_central import FarmaCentralProduct
from app.services.inventory.movements import ProductMovementService
from app.services.inventory.sync import InventorySyncService
from app.services.production.orchestration import produce
from app.services.supply.requests import SupplyRequestService

RawMaterialSource = Literal["supply", "sandbox"]
logger = logging.getLogger(__name__)


class KitProductionError(Exception):
    """Base error for the end-to-end kit workflow."""


class ProductionCatalogError(KitProductionError):
    pass


class ProductionCapacityError(KitProductionError):
    pass


class ProductionOutputNotLinkedError(KitProductionError):
    pass


class KitProductionService:
    def __init__(
        self,
        client: FarmaCentralClient,
        session: Session,
        *,
        raw_material_source: RawMaterialSource,
    ) -> None:
        self._client = client
        self._session = session
        self._raw_material_source = raw_material_source
        self._catalog: dict[str, FarmaCentralProduct] = {}
        self._packaging_code = ""
        self._buffer_code = ""
        self._packaging_capacity = 0
        self._sandbox_requests = 0

    async def produce_all_kits(
        self, *, quantity_per_sku: int = 30
    ) -> dict[str, list[ProductionRun]]:
        await self._initialize()
        kits = sorted(
            (
                product
                for product in self._catalog.values()
                if product.sellable and product.production.at == "packaging"
            ),
            key=lambda product: product.sku,
        )
        if not kits:
            raise ProductionCatalogError("The catalog does not contain sellable kits")

        return await self._produce_kits(kits, quantity_per_sku)

    async def produce_kits(
        self, *, skus: list[str], quantity_per_sku: int
    ) -> dict[str, list[ProductionRun]]:
        await self._initialize()
        kits = []
        for sku in skus:
            product = self._catalog.get(sku)
            if (
                product is None
                or not product.sellable
                or product.production.at != "packaging"
            ):
                raise ProductionCatalogError(f"{sku} is not a sellable kit")
            kits.append(product)
        return await self._produce_kits(kits, quantity_per_sku)

    async def _produce_kits(
        self, kits: list[FarmaCentralProduct], quantity_per_sku: int
    ) -> dict[str, list[ProductionRun]]:
        result: dict[str, list[ProductionRun]] = {}
        for kit in kits:
            logger.info("Producing %s units of %s", quantity_per_sku, kit.sku)
            result[kit.sku] = await self._produce_quantity(
                kit, quantity_per_sku, dependency_path=()
            )
        return result

    async def _initialize(self) -> None:
        await self._synchronize()
        payload = await self._client.get_available_products()
        if not isinstance(payload, list):
            raise FarmaCentralInvalidResponseError("Invalid products: expected a list")
        try:
            products = [FarmaCentralProduct.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise FarmaCentralInvalidResponseError("Invalid product catalog") from exc
        self._catalog = {product.sku: product for product in products}

        spaces = await self._client.get_spaces()
        if not isinstance(spaces, list):
            raise FarmaCentralInvalidResponseError("Invalid spaces: expected a list")
        packaging = next(
            (space for space in spaces if space.get("packaging") is True), None
        )
        buffer = next((space for space in spaces if space.get("buffer") is True), None)
        if packaging is None or buffer is None:
            raise ProductionCatalogError("Packaging and buffer spaces are required")
        capacity = packaging.get("totalSpace")
        if not isinstance(capacity, int) or capacity <= 0:
            raise ProductionCatalogError("Packaging capacity is invalid")
        self._packaging_code = packaging["_id"]
        self._buffer_code = buffer["_id"]
        self._packaging_capacity = capacity

    async def _produce_quantity(
        self,
        product: FarmaCentralProduct,
        quantity: int,
        *,
        dependency_path: tuple[str, ...],
    ) -> list[ProductionRun]:
        if product.sku in dependency_path:
            cycle = " -> ".join((*dependency_path, product.sku))
            raise ProductionCatalogError(f"Production formula cycle: {cycle}")
        if product.production.at != "packaging" or not product.components:
            raise ProductionCatalogError(
                f"{product.sku} does not have a packaging formula"
            )

        batch_size = product.production.batch
        production_quantity = ceil(quantity / batch_size) * batch_size
        max_chunk = self._maximum_chunk_size(product)
        chunks = _split_quantity(production_quantity, max_chunk)
        runs: list[ProductionRun] = []
        availability_times: list[datetime] = []

        await self._reject_ambiguous_pending_runs(product.sku)
        next_path = (*dependency_path, product.sku)
        for chunk in chunks:
            logger.info("Preparing production of %s x %s", chunk, product.sku)
            for component in product.components:
                await self._ensure_available(
                    component.sku,
                    component.required_per_unit * chunk,
                    dependency_path=next_path,
                )

            await self._synchronize()
            await self._clear_packaging()
            for component in product.components:
                await self._stage_component(
                    component.sku,
                    component.required_per_unit * chunk,
                )

            run, supply = await produce(
                self._session,
                client=self._client,
                sku=product.sku,
                quantity=chunk,
            )
            runs.append(run)
            availability_times.append(supply.available_at)
            logger.info(
                "Production run %s accepted; available at %s",
                run.id,
                supply.available_at.isoformat(),
            )

        await _wait_until(max(availability_times))
        await self._wait_for_linked_outputs(runs)
        return runs

    async def _ensure_available(
        self,
        sku: str,
        quantity: int,
        *,
        dependency_path: tuple[str, ...],
    ) -> None:
        available = len(self._available_units(sku))
        if available >= quantity:
            return

        product = self._catalog.get(sku)
        if product is None:
            raise ProductionCatalogError(f"Product {sku} is missing from catalog")
        missing = quantity - available
        if product.production.at == "packaging":
            await self._produce_quantity(
                product,
                missing,
                dependency_path=dependency_path,
            )
        else:
            await self._provision_raw_material(product, missing)

        available_after = len(self._available_units(sku))
        if available_after < quantity:
            raise ProductionOutputNotLinkedError(
                f"Expected {quantity} available units of {sku}, found {available_after}"
            )

    async def _provision_raw_material(
        self, product: FarmaCentralProduct, missing: int
    ) -> None:
        if self._raw_material_source == "sandbox":
            logger.info("Creating %s sandbox units of %s", missing, product.sku)
            for _ in range(missing):
                await self._respect_sandbox_rate_limit()
                await self._client.post("/sandbox/products", {"sku": product.sku})
                self._sandbox_requests += 1
            await self._synchronize()
            return

        batch_size = product.production.batch
        requested_quantity = ceil(missing / batch_size) * batch_size
        logger.info(
            "Requesting %s units of %s from Farma Central",
            requested_quantity,
            product.sku,
        )
        response = await SupplyRequestService(self._client, self._session).request(
            sku=product.sku, quantity=requested_quantity
        )
        await _wait_until(response.available_at)
        await self._synchronize()

    async def _respect_sandbox_rate_limit(self) -> None:
        if self._sandbox_requests and self._sandbox_requests % 200 == 0:
            await asyncio.sleep(65)

    def _maximum_chunk_size(self, product: FarmaCentralProduct) -> int:
        required_space_per_unit = sum(
            component.required_per_unit for component in product.components
        )
        capacity_quantity = self._packaging_capacity // required_space_per_unit
        batch_size = product.production.batch
        maximum = capacity_quantity - (capacity_quantity % batch_size)
        if maximum < batch_size:
            raise ProductionCapacityError(
                f"One batch of {product.sku} requires more than "
                f"{self._packaging_capacity} packaging slots"
            )
        return maximum

    async def _clear_packaging(self) -> None:
        location = self._location(self._packaging_code)
        units = list(
            self._session.scalars(
                select(Unit).where(
                    Unit.current_location_id == location.id,
                    Unit.status == "available",
                )
            )
        )
        movement_service = ProductMovementService(self._client, self._session)
        for unit in units:
            await movement_service.move(
                product_id=unit.external_unit_id,
                destination_store=self._buffer_code,
            )

    async def _stage_component(self, sku: str, quantity: int) -> None:
        units = self._available_units(sku)[:quantity]
        if len(units) != quantity:
            raise ProductionOutputNotLinkedError(
                f"Cannot stage {quantity} units of {sku}; found {len(units)}"
            )
        movement_service = ProductMovementService(self._client, self._session)
        for unit in units:
            await movement_service.move(
                product_id=unit.external_unit_id,
                destination_store=self._packaging_code,
            )

    def _available_units(self, sku: str) -> list[Unit]:
        statement = (
            select(Unit)
            .join(Lot, Unit.lot_id == Lot.id)
            .join(Product, Lot.product_id == Product.id)
            .where(
                Product.sku == sku,
                Unit.status == "available",
                Unit.effective_expires_at > datetime.now(UTC),
            )
            .order_by(Unit.effective_expires_at, Unit.id)
        )
        catalog_product = self._catalog.get(sku)
        if catalog_product is not None and catalog_product.production.at == "packaging":
            statement = statement.where(Lot.produced_by.has())
        return list(self._session.scalars(statement))

    def _location(self, code: str) -> Location:
        location = self._session.scalar(select(Location).where(Location.code == code))
        if location is None:
            raise ProductionCatalogError(f"Location {code} has not been synchronized")
        return location

    async def _synchronize(self) -> None:
        await InventorySyncService(self._client, self._session).synchronize()

    async def _reject_ambiguous_pending_runs(self, sku: str) -> None:
        await self._synchronize()
        pending = find_pending_runs_for_sku(self._session, sku=sku)
        if pending:
            raise ProductionOutputNotLinkedError(
                f"There are {len(pending)} unresolved production runs for {sku}"
            )

    async def _wait_for_linked_outputs(
        self, runs: list[ProductionRun], *, timeout_seconds: int = 600
    ) -> None:
        deadline = datetime.now(UTC).timestamp() + timeout_seconds
        while True:
            await self._synchronize()
            for run in runs:
                self._session.refresh(run)
            if all(
                run.output_lot_id is not None and run.completed_at is not None
                for run in runs
            ):
                return
            if datetime.now(UTC).timestamp() >= deadline:
                unresolved = ", ".join(
                    str(run.id) for run in runs if run.output_lot_id is None
                )
                raise ProductionOutputNotLinkedError(
                    f"Production outputs were not linked before timeout: {unresolved}"
                )
            await asyncio.sleep(2)


def _split_quantity(quantity: int, maximum_chunk: int) -> list[int]:
    chunks = []
    remaining = quantity
    while remaining:
        chunk = min(remaining, maximum_chunk)
        chunks.append(chunk)
        remaining -= chunk
    return chunks


async def _wait_until(available_at: datetime) -> None:
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=UTC)
    seconds = (available_at - datetime.now(UTC)).total_seconds()
    if seconds > 0:
        await asyncio.sleep(seconds + 2)
