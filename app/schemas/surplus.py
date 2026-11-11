from decimal import Decimal

from pydantic import BaseModel, Field

from app.core.scarcity import GROUP3_DIRECT


class SurplusPolicy(BaseModel):
    skus: tuple[str, ...] = tuple(sorted(GROUP3_DIRECT))
    kit_buffer: int = Field(default=2, ge=1)
    raw_buffer_batches: int = Field(default=1, ge=1)
    lot_size: int = Field(default=20, ge=1, le=100)
    minimum_margin: Decimal = Field(
        default=Decimal("0.10"), ge=0, lt=1, allow_inf_nan=False
    )
    minimum_lifetime_seconds: float = Field(default=14400, ge=3660, allow_inf_nan=False)


class SurplusQuote(BaseModel):
    sku: str
    quantity: int = 0
    available: int = 0
    protected: int = 0
    reference_price: Decimal | None = None
    estimated_unit_cost: Decimal | None = None
    unit_price: Decimal | None = None
    reason: str = ""
