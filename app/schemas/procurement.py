from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, Field


class ProcurementDemand(BaseModel):
    sku: str = Field(min_length=1, max_length=50)
    quantity: int = Field(gt=0, le=200, strict=True)
    buffer: int = Field(default=0, ge=0, le=200, strict=True)
    deadline: AwareDatetime
    expected_revenue: Decimal = Field(gt=0, allow_inf_nan=False)
    # Include incurred/committed costs not represented by the new acquisition plan.
    other_costs: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    minimum_margin: Decimal = Field(
        default=Decimal("0.10"), ge=0, lt=1, allow_inf_nan=False
    )

    @property
    def budget(self) -> Decimal:
        return self.expected_revenue * (1 - self.minimum_margin) - self.other_costs


class ProcurementAction(BaseModel):
    kind: str
    sku: str
    quantity: int
    useful_quantity: int
    cost: Decimal
    offer_id: str | None = None


class ProcurementPlan(BaseModel):
    status: str
    cost: Decimal = Decimal(0)
    seconds: float = 0
    actions: list[ProcurementAction] = Field(default_factory=list)
    reason: str = ""
