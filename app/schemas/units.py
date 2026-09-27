from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class UnitData:
    external_unit_id: str
    lot_external_id: str
    location_code: str
    status: str


class UnitResponse(BaseModel):
    external_unit_id: str
    status: str
