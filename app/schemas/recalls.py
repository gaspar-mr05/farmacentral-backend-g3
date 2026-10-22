from typing import Annotated

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

NonBlankString = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class RecallWebhookPayload(BaseModel):
    recall_id: NonBlankString = Field(alias="recallId")
    batch: NonBlankString
    sku: NonBlankString
    reason: NonBlankString
    issued_at: AwareDatetime = Field(alias="issuedAt")
    deadline: AwareDatetime

    @model_validator(mode="after")
    def validate_deadline(self) -> "RecallWebhookPayload":
        if self.deadline <= self.issued_at:
            raise ValueError("deadline must be after issuedAt")
        return self
