from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import OrderSource, OrderStatus


class OrderItemCreate(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1)


class OrderCreate(BaseModel):
    buyer_name: str = Field(min_length=1)
    buyer_email: str = Field(min_length=3)
    source: OrderSource = OrderSource.WEB
    items: list[OrderItemCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_skus(self) -> "OrderCreate":
        skus = [item.sku for item in self.items]
        if len(skus) != len(set(skus)):
            raise ValueError("Order items cannot contain duplicate SKUs")
        return self


class OrderItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sku: str
    quantity: int
    unit_price: int
    line_total: int


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    buyer_name: str
    buyer_email: str
    source: OrderSource
    status: OrderStatus
    total: int
    items: list[OrderItemResponse]
    created_at: datetime
