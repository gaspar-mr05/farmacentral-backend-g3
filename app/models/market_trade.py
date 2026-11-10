import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.order import Order


class MarketRole(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"


class MarketTrade(Base, TimestampMixin):
    """A confirmed market closure; open offers and unit reservations come later."""

    __tablename__ = "market_trades"
    __table_args__ = (
        UniqueConstraint("id", "period_id", name="uq_market_trades_id_period"),
        UniqueConstraint(
            "period_id", "external_offer_id", name="uq_market_trade_offer"
        ),
        UniqueConstraint(
            "period_id", "external_order_id", name="uq_market_trade_order"
        ),
        UniqueConstraint("period_id", "sale_order_id", name="uq_market_trade_sale"),
        CheckConstraint("length(btrim(external_offer_id)) > 0", name="ck_trade_offer"),
        CheckConstraint("length(btrim(external_order_id)) > 0", name="ck_trade_order"),
        CheckConstraint(
            "length(btrim(counterparty_id)) > 0", name="ck_trade_counterparty"
        ),
        CheckConstraint("length(btrim(sku)) > 0", name="ck_trade_sku"),
        CheckConstraint("quantity > 0", name="ck_trade_quantity"),
        CheckConstraint(
            "unit_price >= 0 AND unit_price < 'Infinity'::numeric",
            name="ck_trade_price",
        ),
        CheckConstraint(
            "role = 'SELLER' OR sale_order_id IS NULL", name="ck_trade_sale_role"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_periods.id"), nullable=False
    )
    external_offer_id: Mapped[str] = mapped_column(String, nullable=False)
    external_order_id: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[MarketRole] = mapped_column(Enum(MarketRole), nullable=False)
    counterparty_id: Mapped[str] = mapped_column(String, nullable=False)
    sku: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sale_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))

    sale_order: Mapped["Order | None"] = relationship()

    @property
    def total(self) -> Decimal:
        return Decimal(self.unit_price) * self.quantity
