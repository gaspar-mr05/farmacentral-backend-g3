from dataclasses import dataclass
from datetime import datetime

from app.models import LotOrigin, ProductCategory


@dataclass(frozen=True)
class ProductData:
    sku: str
    name: str
    category: ProductCategory
    batch_size: int
    requires_refrigeration: bool


@dataclass(frozen=True)
class LocationData:
    code: str
    name: str
    is_refrigerated: bool


@dataclass(frozen=True)
class LotData:
    external_lot_id: str
    product_sku: str
    expires_at: datetime
    origin: LotOrigin


@dataclass(frozen=True)
class UnitData:
    external_unit_id: str
    lot_external_id: str
    location_code: str
    status: str


@dataclass(frozen=True)
class InventoryData:
    products: tuple[ProductData, ...]
    locations: tuple[LocationData, ...]
    lots: tuple[LotData, ...]
    units: tuple[UnitData, ...]
