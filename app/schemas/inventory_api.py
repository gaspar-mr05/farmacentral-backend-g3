from pydantic import BaseModel

from app.schemas.locations import LocationResponse
from app.schemas.lots import LotResponse
from app.schemas.products import ProductResponse
from app.schemas.units import UnitResponse


class InventoryItemResponse(BaseModel):
    unit: UnitResponse
    product: ProductResponse
    lot: LotResponse
    location: LocationResponse
