from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import LotOrigin, MarketRole


class TraceabilityLotResponse(BaseModel):
    id: UUID
    external_lot_id: str
    product_sku: str
    product_name: str
    origin: LotOrigin
    expires_at: datetime | None
    quantity: int = Field(ge=0)
    requires_refrigeration: bool


class TraceabilityUnitLocationResponse(BaseModel):
    code: str
    name: str


class TraceabilityUnitResponse(BaseModel):
    external_unit_id: str
    status: str
    effective_expires_at: datetime
    location: TraceabilityUnitLocationResponse


class ProductionLinkResponse(BaseModel):
    production_run_id: UUID
    input_lot_id: UUID
    output_lot_id: UUID
    quantity_consumed: int = Field(ge=1)
    consumed_unit_ids: list[str] = Field(default_factory=list)
    requested_at: datetime
    completed_at: datetime | None


class TraceabilityDeliveryResponse(BaseModel):
    lot_id: UUID
    order_id: UUID | str
    buyer_name: str
    buyer_email: str | None
    sku: str
    external_unit_id: str
    quantity: int = 1
    dispatched_at: datetime


class TraceabilityMarketTransferResponse(BaseModel):
    trade_id: UUID
    external_offer_id: str
    external_order_id: str
    role: MarketRole
    counterparty_id: str
    lot_id: UUID
    sku: str
    external_unit_id: str
    occurred_at: datetime


class TraceabilityResponse(BaseModel):
    lot: TraceabilityLotResponse
    current_units: list[TraceabilityUnitResponse]
    ancestors: list[TraceabilityLotResponse]
    descendants: list[TraceabilityLotResponse]
    production_links: list[ProductionLinkResponse]
    deliveries: list[TraceabilityDeliveryResponse] = Field(default_factory=list)
    market_transfers: list[TraceabilityMarketTransferResponse] = Field(
        default_factory=list
    )
