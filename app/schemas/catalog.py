from datetime import datetime

from pydantic import BaseModel, Field


class MarketPrice(BaseModel):
    sku: str = Field(min_length=1)
    price: int = Field(ge=0)
    fair_value: int = Field(alias="fairValue", ge=0)
    updated_at: datetime = Field(alias="updatedAt")


class CatalogLotResponse(BaseModel):
    external_lot_id: str = Field(min_length=1)
    stock: int = Field(ge=1)
    next_expiry_at: datetime


class CatalogItemResponse(BaseModel):
    sku: str = Field(min_length=1)
    name: str = Field(min_length=1)
    price: int = Field(ge=0)
    stock: int = Field(ge=0)
    next_expiry_at: datetime | None
    lots: list[CatalogLotResponse]
    price_updated_at: datetime
