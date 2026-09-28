from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.models import LotOrigin, ProductCategory
from app.schemas.farma_central import (
    FarmaCentralProduct,
    FarmaCentralSpace,
    FarmaCentralUnit,
)
from app.schemas.inventory import InventoryData
from app.schemas.locations import LocationData
from app.schemas.lots import LotData
from app.schemas.products import ProductData
from app.schemas.units import UnitData


def normalize_inventory(
    catalog: list[FarmaCentralProduct],
    spaces: list[FarmaCentralSpace],
    external_units: list[FarmaCentralUnit],
) -> InventoryData:
    """Converts external contracts into persistence-oriented inventory data."""
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
        lot_id = unit.batch or f"unreported:{unit.external_id}"
        lot = LotData(lot_id, unit.sku, unit.expires_at, LotOrigin.FARMA_CENTRAL)
        existing_lot = lots.get(lot_id)
        if existing_lot is not None and existing_lot.product_sku != unit.sku:
            raise FarmaCentralInvalidResponseError(
                f"Lot {lot_id} has inconsistent product data"
            )
        if existing_lot is None or lot.expires_at < existing_lot.expires_at:
            lots[lot_id] = lot

        normalized_unit = UnitData(
            external_unit_id=unit.external_id,
            lot_external_id=lot_id,
            location_code=unit.store_id,
            status="available",
            effective_expires_at=unit.expires_at,
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
