from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints

NonBlankString = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class MessageSender(BaseModel):
    name: NonBlankString
    phone: NonBlankString


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


class MessagingResponse(BaseModel):
    order_id: str | None = Field(
        default=None, alias="orderId", pattern=r"^[0-9a-fA-F]{24}$"
    )
    status: Literal["discarded"] | None = None
    test: bool = False
