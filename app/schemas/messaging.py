from typing import Annotated

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
