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
