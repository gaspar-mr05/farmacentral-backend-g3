from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel


@dataclass(frozen=True)
class UnitData:
    external_unit_id: str
    lot_external_id: str
    location_code: str
    status: str
    effective_expires_at: datetime


class UnitResponse(BaseModel):
    external_unit_id: str
    status: str
