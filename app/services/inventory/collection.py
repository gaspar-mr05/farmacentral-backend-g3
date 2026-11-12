import asyncio
import logging

from pydantic import BaseModel, ValidationError

from app.clients.farma_central import (
    FarmaCentralClient,
    FarmaCentralInvalidResponseError,
    JSONResponse,
)
from app.schemas.farma_central import (
    FarmaCentralInventoryItem,
    FarmaCentralProduct,
    FarmaCentralSpace,
    FarmaCentralUnit,
)

logger = logging.getLogger(__name__)

CollectedInventory = tuple[
    list[FarmaCentralProduct],
    list[FarmaCentralSpace],
    list[FarmaCentralUnit],
    set[tuple[str, str]],
]


class InventorySnapshotChangedError(FarmaCentralInvalidResponseError):
    """The stock count changed between the inventory and unit-list GETs."""


class InventoryCollector:
    """Reads and validates the inventory exposed by Farma Central."""

    def __init__(self, client: FarmaCentralClient, *, tolerate_changes=False) -> None:
        self._client = client
        self._tolerate_changes = tolerate_changes
        self._semaphore = asyncio.Semaphore(4)

    async def _read(self, method, *args, **kwargs):
        async with self._semaphore:
            return await method(*args, **kwargs)

    async def collect(self) -> CollectedInventory:
        catalog_payload, spaces_payload = await asyncio.gather(
            self._client.get_available_products(),
            self._client.get_spaces(),
        )
        catalog = _parse_list(catalog_payload, FarmaCentralProduct, "products")
        spaces = _parse_list(spaces_payload, FarmaCentralSpace, "spaces")
        units, incomplete_groups = await self._collect_units(spaces)
        return catalog, spaces, units, incomplete_groups

    async def _collect_units(
        self,
        spaces: list[FarmaCentralSpace],
    ) -> tuple[list[FarmaCentralUnit], set[tuple[str, str]]]:
        inventory_payloads = await asyncio.gather(
            *(
                self._read(self._client.get_space_inventory, space.external_id)
                for space in spaces
            )
        )
        inventories = [
            _parse_list(payload, FarmaCentralInventoryItem, "inventory")
            for payload in inventory_payloads
        ]
        requests = [
            (space, item)
            for space, inventory in zip(spaces, inventories, strict=True)
            for item in inventory
            if item.quantity > 0
        ]

        async def collect_group(space, original):
            item = original
            for attempt in range(3 if self._tolerate_changes else 1):
                payload = await self._read(
                    self._client.get_space_products,
                    space.external_id,
                    item.sku,
                    limit=max(1, min(item.quantity, 200)),
                )
                observed = _parse_list(payload, FarmaCentralUnit, "space products")
                try:
                    _validate_units(space, item, observed)
                    return observed, item.quantity > len(observed) or attempt > 0
                except InventorySnapshotChangedError:
                    if not self._tolerate_changes:
                        raise
                    if attempt == 2:
                        logger.warning(
                            "Inventory group remains unstable: sku=%s space=%s",
                            item.sku,
                            space.external_id,
                        )
                        return [], True
                    current = _parse_list(
                        await self._read(
                            self._client.get_space_inventory, space.external_id
                        ),
                        FarmaCentralInventoryItem,
                        "inventory",
                    )
                    item = next(
                        (i for i in current if i.sku == item.sku),
                        FarmaCentralInventoryItem(sku=item.sku, quantity=0),
                    )
            raise AssertionError("Bounded group retry exhausted")

        results = await asyncio.gather(
            *(collect_group(space, item) for space, item in requests)
        )
        units = []
        incomplete_groups = set()
        for (space, item), (observed, incomplete) in zip(
            requests, results, strict=True
        ):
            units.extend(observed)
            if incomplete:
                if self._tolerate_changes:
                    incomplete_groups.update((s.external_id, item.sku) for s in spaces)
                else:
                    incomplete_groups.add((space.external_id, item.sku))
        by_id = {}
        ambiguous = set()
        for unit in units:
            previous = by_id.get(unit.external_id)
            if previous is not None:
                if (previous.sku, previous.batch, previous.expires_at) != (
                    unit.sku,
                    unit.batch,
                    unit.expires_at,
                ):
                    raise FarmaCentralInvalidResponseError(
                        "Conflicting unit identity between inventory groups"
                    )
                if not self._tolerate_changes:
                    raise InventorySnapshotChangedError(
                        "Unit moved between inventory groups"
                    )
                ambiguous.add(unit.external_id)
                incomplete_groups.update((s.external_id, unit.sku) for s in spaces)
            by_id[unit.external_id] = unit
        return [
            u for uid, u in by_id.items() if uid not in ambiguous
        ], incomplete_groups

    async def collect_units_for_skus(
        self,
        spaces: list[FarmaCentralSpace],
        skus: set[str],
    ) -> list[FarmaCentralUnit]:
        requests = [(space, sku) for space in spaces for sku in sorted(skus)]
        payloads = await asyncio.gather(
            *(
                self._read(
                    self._client.get_space_products,
                    space.external_id,
                    sku,
                    limit=200,
                )
                for space, sku in requests
            )
        )
        units = []
        for (space, sku), payload in zip(requests, payloads, strict=True):
            observed = _parse_list(payload, FarmaCentralUnit, "space products")
            if any(
                u.sku != sku or u.store_id != space.external_id for u in observed
            ) or len({u.external_id for u in observed}) != len(observed):
                raise FarmaCentralInvalidResponseError(
                    "Invalid production inventory identity or space"
                )
            units.extend(observed)
        return units


def _parse_list[Schema: BaseModel](
    payload: JSONResponse,
    schema: type[Schema],
    resource: str,
) -> list[Schema]:
    if not isinstance(payload, list):
        raise FarmaCentralInvalidResponseError(f"Invalid {resource}: expected a list")
    try:
        return [schema.model_validate(item) for item in payload]
    except ValidationError as exc:
        raise FarmaCentralInvalidResponseError(f"Invalid {resource}") from exc


def _validate_units(
    space: FarmaCentralSpace,
    item: FarmaCentralInventoryItem,
    units: list[FarmaCentralUnit],
) -> None:
    correct_count = len(units) == min(item.quantity, 200)
    correct_origin = all(
        unit.sku == item.sku and unit.store_id == space.external_id for unit in units
    )
    if not correct_origin or len({unit.external_id for unit in units}) != len(units):
        raise FarmaCentralInvalidResponseError(
            f"Inventory mismatch for SKU {item.sku} in space {space.external_id}"
        )
    if not correct_count:
        raise InventorySnapshotChangedError(
            f"Inventory changed for SKU {item.sku} in space {space.external_id}: "
            f"expected {min(item.quantity, 200)} units, observed {len(units)}"
        )
