from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models import (
    ExternalOrderStatus,
    InvoiceSide,
    InvoiceStatus,
    MarketOfferSide,
    OrderStatus,
)
from app.schemas.business_metrics import BusinessMetrics


class OperationsPage[T](BaseModel):
    period_id: UUID
    read_at: datetime
    data_source: Literal["persisted"] = "persisted"
    items: list[T]
    limit: int
    offset: int
    has_more: bool


class WaitingOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    external_order_id: str
    status: OrderStatus
    external_status: ExternalOrderStatus | None
    decision_reason: str | None
    delivery_deadline: datetime | None
    created_at: datetime
    updated_at: datetime


class OperationalOfferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    external_offer_id: str | None
    external_order_id: str | None
    side: MarketOfferSide
    publisher_group_id: int
    counterparty_group_id: int | None
    sku: str
    quantity: int
    unit_price: Decimal
    status: str
    updated_at: datetime
    last_checked_at: datetime | None
    reconciled_at: datetime | None
    next_retry_at: datetime | None
    http_status: int | None
    has_error: bool


class OperationalInvoiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    external_invoice_id: str
    external_order_id: str | None
    side: InvoiceSide
    counterparty_id: str
    channel: str
    status: InvoiceStatus
    principal: Decimal
    interest: Decimal
    total: Decimal
    issued_at: datetime
    external_updated_at: datetime
    observed_at: datetime
    paid_at: datetime | None


class OperationsMetricsResponse(BaseModel):
    period_id: UUID
    read_at: datetime
    data_source: Literal["persisted"] = "persisted"
    state: Literal["not_captured", "captured"]
    captured_at: datetime | None
    commit_sha: str | None
    metrics: BusinessMetrics | None
