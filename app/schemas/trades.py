from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
)

TradeOfferId = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{24}$")]
TradeSide = Literal["buy", "sell"]
TradeState = Literal["open", "closing", "closed", "cancelled", "expired"]
TradeText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
]
TradeMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class TradeOfferCreate(BaseModel):
    side: TradeSide
    sku: TradeText
    quantity: int = Field(gt=0, strict=True)
    unit_price: TradeMoney = Field(alias="unitPrice")

    @field_serializer("unit_price", when_used="json")
    def serialize_unit_price(self, value: Decimal) -> float:
        return float(value)

    def validate_market_price(self, current_unit_price: Decimal) -> None:
        if current_unit_price <= 0:
            raise ValueError("Current market price must be positive")
        minimum = current_unit_price * Decimal("0.5")
        maximum = current_unit_price * Decimal("1.5")
        if not minimum <= self.unit_price <= maximum:
            raise ValueError("Offer price must be between 0.5x and 1.5x market price")


class TradeOffer(BaseModel):
    """Published offer contract; unknown evidence fields remain available."""

    model_config = ConfigDict(extra="allow")

    id: TradeOfferId = Field(validation_alias=AliasChoices("id", "_id", "offerId"))
    side: TradeSide
    sku: TradeText
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(alias="unitPrice", gt=0, allow_inf_nan=False)
    status: TradeState = Field(
        validation_alias=AliasChoices("state", "status"), serialization_alias="state"
    )
    order_id: str | None = Field(default=None, alias="orderId")
    publisher_group: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "publisher", "publisherGroup", "group", "ownerGroup"
        ),
        gt=0,
    )
    counterparty_group: int | None = Field(default=None, alias="counterparty", gt=0)
    closed_at: AwareDatetime | None = Field(default=None, alias="closedAt")
    created_at: AwareDatetime | None = Field(default=None, alias="createdAt")
    expires_at: AwareDatetime | None = Field(default=None, alias="expiresAt")

    @property
    def lifetime(self) -> timedelta | None:
        if self.created_at is None or self.expires_at is None:
            return None
        return self.expires_at - self.created_at
