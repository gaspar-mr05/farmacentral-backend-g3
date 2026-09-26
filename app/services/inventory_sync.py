import asyncio

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient, JSONResponse
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.db.inventory import InventorySyncResult, sync_inventory
from app.models import LotOrigin, ProductCategory
from app.schemas.farma_central import (
    FarmaCentralInventoryItem,
    FarmaCentralProduct,
    FarmaCentralSpace,
    FarmaCentralUnit,
)
from app.schemas.inventory import (
    InventoryData,
    LocationData,
    LotData,
    ProductData,
    UnitData,
)


class InventorySyncService:
    def __init__(self, client: FarmaCentralClient, session: Session) -> None:
        self._client = client
        self._session = session

    async def synchronize(self) -> InventorySyncResult:
        catalog, spaces, units = await self._collect_inventory()
        data = _normalize_inventory(catalog, spaces, units)
        try:
            result = sync_inventory(self._session, data)
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise

    async def _collect_inventory(
        self,
    ) -> tuple[
        list[FarmaCentralProduct],
        list[FarmaCentralSpace],
        list[FarmaCentralUnit],
    ]:
        catalog_payload, spaces_payload = await asyncio.gather(
            self._client.get_available_products(),
            self._client.get_spaces(),
        )
        catalog = _parse_list(catalog_payload, FarmaCentralProduct, "products")
        spaces = _parse_list(spaces_payload, FarmaCentralSpace, "spaces")
        units = await self._collect_units(spaces)
        return catalog, spaces, units

    async def _collect_units(
        self, spaces: list[FarmaCentralSpace]
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


def _normalize_inventory(
    catalog: list[FarmaCentralProduct],
    spaces: list[FarmaCentralSpace],
    external_units: list[FarmaCentralUnit],
) -> InventoryData:
    products = tuple(_normalize_product(product) for product in catalog)
    locations = tuple(_normalize_location(space) for space in spaces)
    lots, units = _normalize_stock(
        external_units,
        {product.sku for product in products},
        {location.code for location in locations},
    )
    return InventoryData(products, locations, lots, units)


def _normalize_product(product: FarmaCentralProduct) -> ProductData:
    if product.production.at == "farma-central":
        category = ProductCategory.INSUMO
    elif product.sellable:
        category = ProductCategory.KIT
    else:
        category = ProductCategory.ACONDICIONADO
    return ProductData(
        sku=product.sku,
        name=product.name,
        category=category,
        batch_size=product.production.batch,
        requires_refrigeration=product.storage.cold,
    )


def _normalize_location(space: FarmaCentralSpace) -> LocationData:
    roles = []
    if space.check_in:
        roles.append("Recepción")
    if space.check_out:
        roles.append("Despacho")
    if space.packaging:
        roles.append("Acondicionamiento")
    if space.buffer:
        roles.append("Bodega externa")
    if space.quarantine:
        roles.append("Cuarentena")
    if not roles:
        roles.append("Almacenamiento refrigerado" if space.cold else "Almacenamiento")
    return LocationData(space.external_id, " / ".join(roles), space.cold)


def _normalize_stock(
    external_units: list[FarmaCentralUnit],
    product_skus: set[str],
    location_codes: set[str],
) -> tuple[tuple[LotData, ...], tuple[UnitData, ...]]:
    lots: dict[str, LotData] = {}
    units: dict[str, UnitData] = {}
    for unit in external_units:
        _validate_references(unit, product_skus, location_codes)
        lot = LotData(unit.batch, unit.sku, unit.expires_at, LotOrigin.FARMA_CENTRAL)
        existing_lot = lots.get(unit.batch)
        if existing_lot is not None and existing_lot.product_sku != unit.sku:
            raise FarmaCentralInvalidResponseError(
                f"Lot {unit.batch} has inconsistent product data"
            )
        if existing_lot is None or lot.expires_at < existing_lot.expires_at:
            lots[unit.batch] = lot

        normalized_unit = UnitData(
            unit.external_id, unit.batch, unit.store_id, "available"
        )
        if unit.external_id in units and units[unit.external_id] != normalized_unit:
            raise FarmaCentralInvalidResponseError(
                f"Unit {unit.external_id} has inconsistent data"
            )
        units[unit.external_id] = normalized_unit
    return tuple(lots.values()), tuple(units.values())


def _validate_references(
    unit: FarmaCentralUnit,
    product_skus: set[str],
    location_codes: set[str],
) -> None:
    if unit.sku not in product_skus:
        raise FarmaCentralInvalidResponseError(
            f"Unit {unit.external_id} has unknown SKU {unit.sku}"
        )
    if unit.store_id not in location_codes:
        raise FarmaCentralInvalidResponseError(
            f"Unit {unit.external_id} has unknown space {unit.store_id}"
        )
