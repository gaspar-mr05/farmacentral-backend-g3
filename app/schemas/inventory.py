from dataclasses import dataclass

from app.schemas.locations import LocationData
from app.schemas.lots import LotData
from app.schemas.products import ProductData
from app.schemas.units import UnitData


@dataclass(frozen=True)
class InventoryData:
    products: tuple[ProductData, ...]
    locations: tuple[LocationData, ...]
    lots: tuple[LotData, ...]
    units: tuple[UnitData, ...]
