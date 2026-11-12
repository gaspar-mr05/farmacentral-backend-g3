import re
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
)

from app.schemas.purchase_orders import PurchaseOrderId

NonBlankString = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class MessageSender(BaseModel):
    name: NonBlankString
    phone: NonBlankString

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if value.strip().startswith("00"):
            digits = digits[2:]
        if not 8 <= len(digits) <= 15:
            raise ValueError("phone must contain between 8 and 15 digits")
        return value


class MessagingWebhookPayload(BaseModel):
    message_id: NonBlankString = Field(alias="messageId")
    sender: MessageSender
    text: NonBlankString
    received_at: AwareDatetime = Field(alias="receivedAt")


class MessagingOrderInterpretation(BaseModel):
    sku: NonBlankString
    quantity: int = Field(gt=0)
    delivery_date: AwareDatetime = Field(alias="deliveryDate")


class MessagingOrderRequest(BaseModel):
    status: Literal["order"] = "order"
    interpretation: MessagingOrderInterpretation


class MessagingDiscardRequest(BaseModel):
    status: Literal["discarded"] = "discarded"
    reason: NonBlankString


class MessagingInquiryRequest(BaseModel):
    status: Literal["inquiry"] = "inquiry"
    order_id: PurchaseOrderId = Field(alias="orderId")


class MessagingSendRequest(BaseModel):
    to: NonBlankString
    reply_to: NonBlankString = Field(alias="replyTo")
    text: NonBlankString


class MessagingResponse(BaseModel):
    order_id: str | None = Field(
        default=None, alias="orderId", pattern=r"^[0-9a-fA-F]{24}$"
    )
    status: Literal["order", "inquiry", "discarded"] | None = None
    test: bool = Field(default=False, strict=True)
