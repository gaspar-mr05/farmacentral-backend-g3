from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class LocationData:
    code: str
    name: str
    is_refrigerated: bool
    is_sellable: bool = True


class LocationResponse(BaseModel):
    code: str
    name: str
    is_refrigerated: bool
