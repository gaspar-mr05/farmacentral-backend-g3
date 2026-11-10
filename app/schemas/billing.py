from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

from app.models.billing import InvoiceSide, InvoiceStatus

BillingId = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"\S")
]
BillingMoney = Annotated[Decimal, Field(allow_inf_nan=False)]


class BillingInvoice(BaseModel):
    invoice_id: BillingId = Field(alias="id")
    client_id: BillingId = Field(alias="client")
    supplier_id: BillingId = Field(alias="supplier")
    channel: BillingId
    description: str
    status: InvoiceStatus
    principal: BillingMoney = Field(alias="price", ge=0)
    interest: BillingMoney = Field(ge=0)
    total: BillingMoney = Field(alias="totalPrice", ge=0)
    issued_at: AwareDatetime = Field(alias="createdAt")
    updated_at: AwareDatetime = Field(alias="updatedAt")


class BankStatement(BaseModel):
    group: int = Field(gt=0)
    balance: BillingMoney


class BillingInvoiceQuery(BaseModel):
    status: InvoiceStatus
    side: InvoiceSide
    from_date: AwareDatetime
    to_date: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> "BillingInvoiceQuery":
        if not timedelta(0) <= self.to_date - self.from_date <= timedelta(days=3):
            raise ValueError(
                "Billing invoice range must be between zero and three days"
            )
        return self
