from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from app.models import LotOrigin


@dataclass(frozen=True)
class LotData:
    external_lot_id: str
    product_sku: str
    expires_at: datetime | None
    origin: LotOrigin


class LotResponse(BaseModel):
    external_lot_id: str
    expires_at: datetime | None
