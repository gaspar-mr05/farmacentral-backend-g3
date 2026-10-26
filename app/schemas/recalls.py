from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

NonBlankString = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
ConnectionTestId = Annotated[
    str, StringConstraints(min_length=6, pattern=r"^test_\S+$")
]


class RecallConnectionTestPayload(BaseModel):
    """Fixed connectivity probe; it intentionally identifies no real lot."""

    recall_id: ConnectionTestId = Field(alias="recallId")
    batch: None = None
    sku: None = None
    reason: None = None
    issued_at: AwareDatetime = Field(alias="issuedAt")
    deadline: AwareDatetime

    @model_validator(mode="after")
    def validate_deadline(self) -> "RecallConnectionTestPayload":
        if self.deadline <= self.issued_at:
            raise ValueError("deadline must be after issuedAt")
        return self


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


class RecallNotificationRequest(BaseModel):
    order_id: NonBlankString = Field(alias="orderId")
    customer_name: NonBlankString = Field(alias="customerName")
    product_ids: list[NonBlankString] = Field(alias="productIds", min_length=1)
    message: NonBlankString | None = None


class RecallStockReport(BaseModel):
    product_id: NonBlankString = Field(alias="productId")
    batch: NonBlankString
    store: NonBlankString


class RecallDerivedBatchReport(BaseModel):
    batch: NonBlankString
    sku: NonBlankString
    source_batch: NonBlankString = Field(alias="sourceBatch")


class RecallDeliveryReport(BaseModel):
    order_id: NonBlankString = Field(alias="orderId")
    customer_name: NonBlankString = Field(alias="customerName")
    product_ids: list[NonBlankString] = Field(alias="productIds", min_length=1)


class RecallReportRequest(BaseModel):
    in_stock: list[RecallStockReport] = Field(alias="inStock")
    derived_batches: list[RecallDerivedBatchReport] = Field(alias="derivedBatches")
    deliveries: list[RecallDeliveryReport]


class RecallSummaryResponse(BaseModel):
    recall_id: str
    batch: str
    sku: str
    reason: str
    issued_at: datetime
    deadline: datetime
    status: str
    reported_at: datetime | None


class RecalledUnitResponse(BaseModel):
    product_id: str
    sku: str
    batch: str
    original_location: str
    current_location: str
    quarantined_at: datetime | None


class RecallDetailResponse(RecallSummaryResponse):
    units: list[RecalledUnitResponse]
