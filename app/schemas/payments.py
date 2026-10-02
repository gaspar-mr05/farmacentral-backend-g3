from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models import PaymentStatus


class PaymentStartResponse(BaseModel):
    id: UUID
    order_id: UUID
    external_transaction_id: str
    amount: int
    status: PaymentStatus
    payment_url: str


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    order_id: UUID
    external_transaction_id: str
    amount: int
    status: PaymentStatus
    created_at: datetime
