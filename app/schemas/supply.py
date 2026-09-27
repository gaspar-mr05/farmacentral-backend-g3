from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SupplyRequest(BaseModel):
    sku: Sku
    quantity: int = Field(ge=1, le=5000)


class SupplyResponse(BaseModel):
    sku: str
    quantity: int
    available_at: datetime
