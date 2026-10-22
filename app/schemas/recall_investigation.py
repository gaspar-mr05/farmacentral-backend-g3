from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class RecallLotFinding(BaseModel):
    lot_id: UUID
    external_lot_id: str
    sku: str
    generation: int = Field(ge=0)
    parent_lot_id: UUID | None


class RecallLineageLink(BaseModel):
    input_lot_id: UUID
    output_lot_id: UUID
    production_run_id: UUID


class RecallStockFinding(BaseModel):
    unit_id: UUID
    external_unit_id: str
    lot_id: UUID
    sku: str
    status: str
    location_id: UUID
    location_code: str
    original_location_id: UUID
    original_location_code: str


class RecallDeliveryFinding(BaseModel):
    order_unit_id: UUID
    unit_id: UUID
    external_unit_id: str
    lot_id: UUID
    sku: str
    dispatched_at: datetime


class RecallOrderFinding(BaseModel):
    order_id: UUID
    external_order_id: str | None
    external_customer_id: str | None
    customer_reference: str
    buyer_name: str
    buyer_email: str | None
    deliveries: list[RecallDeliveryFinding]


class RecallInvestigation(BaseModel):
    recall_id: str
    origin_lot_id: UUID
    lots: list[RecallLotFinding]
    production_links: list[RecallLineageLink]
    stock_units: list[RecallStockFinding]
    orders: list[RecallOrderFinding]
    elapsed_ms: float = Field(default=0, ge=0)
