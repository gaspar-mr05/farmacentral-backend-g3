from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FarmaCentralProduction(BaseModel):
    batch: int = Field(gt=0)
    at: Literal["farma-central", "packaging"]


class FarmaCentralStorage(BaseModel):
    cold: bool = False


class FarmaCentralProduct(BaseModel):
    sku: str = Field(min_length=1)
    name: str = Field(min_length=1)
    production: FarmaCentralProduction
    sellable: bool
    storage: FarmaCentralStorage = Field(default_factory=FarmaCentralStorage)


class FarmaCentralSpace(BaseModel):
    external_id: str = Field(alias="_id", min_length=1)
    buffer: bool = False
    packaging: bool = False
    check_in: bool = Field(default=False, alias="checkIn")
    check_out: bool = Field(default=False, alias="checkOut")
    quarantine: bool = False
    cold: bool = False


class FarmaCentralInventoryItem(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=0)


class FarmaCentralUnit(BaseModel):
    external_id: str = Field(alias="_id", min_length=1)
    sku: str = Field(min_length=1)
    store_id: str = Field(alias="store", min_length=1)
    expires_at: datetime = Field(alias="expiresAt")
    batch: str = Field(min_length=1)
