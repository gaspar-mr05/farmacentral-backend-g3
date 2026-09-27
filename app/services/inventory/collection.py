import asyncio

from pydantic import BaseModel, ValidationError

from app.clients.farma_central import FarmaCentralClient, JSONResponse
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.schemas.farma_central import (
    FarmaCentralInventoryItem,
    FarmaCentralProduct,
    FarmaCentralSpace,
    FarmaCentralUnit,
)

CollectedInventory = tuple[
    list[FarmaCentralProduct],
    list[FarmaCentralSpace],
    list[FarmaCentralUnit],
]


class InventoryCollector:
    """Reads and validates the inventory exposed by Farma Central."""

    def __init__(self, client: FarmaCentralClient) -> None:
        self._client = client

    async def collect(self) -> CollectedInventory:
        catalog_payload, spaces_payload = await asyncio.gather(
            self._client.get_available_products(),
            self._client.get_spaces(),
        )
        catalog = _parse_list(catalog_payload, FarmaCentralProduct, "products")
        spaces = _parse_list(spaces_payload, FarmaCentralSpace, "spaces")
        units = await self._collect_units(spaces)
        return catalog, spaces, units

    async def _collect_units(
        self,
        spaces: list[FarmaCentralSpace],
    ) -> list[FarmaCentralUnit]:
        inventory_payloads = await asyncio.gather(
            *(self._client.get_space_inventory(space.external_id) for space in spaces)
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
        product_payloads = await asyncio.gather(
            *(
                self._client.get_space_products(space.external_id, item.sku)
                for space, item in requests
            )
        )

        units = []
        for (space, item), payload in zip(requests, product_payloads, strict=True):
            space_units = _parse_list(payload, FarmaCentralUnit, "space products")
            _validate_units(space, item, space_units)
            units.extend(space_units)
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
    correct_count = len(units) == item.quantity
    correct_origin = all(
        unit.sku == item.sku and unit.store_id == space.external_id for unit in units
    )
    if not correct_count or not correct_origin:
        raise FarmaCentralInvalidResponseError(
            f"Inventory mismatch for SKU {item.sku} in space {space.external_id}"
        )
