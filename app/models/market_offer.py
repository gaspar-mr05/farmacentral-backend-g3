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
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.business_period import BusinessPeriod
    from app.models.unit import Unit


class MarketOfferSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class MarketOffer(Base, TimestampMixin):
    """Local state for both published and accepted market offers."""

    __tablename__ = "market_offers"
    __table_args__ = (
        UniqueConstraint(
            "period_id", "external_offer_id", name="uq_market_offer_external"
        ),
        CheckConstraint("publisher_group_id > 0", name="ck_market_offer_publisher"),
        CheckConstraint(
            "counterparty_group_id IS NULL OR counterparty_group_id > 0",
            name="ck_market_offer_counterparty",
        ),
        CheckConstraint("length(btrim(sku)) > 0", name="ck_market_offer_sku"),
        CheckConstraint("quantity > 0", name="ck_market_offer_quantity"),
        CheckConstraint(
            "unit_price > 0 AND unit_price < 'Infinity'::numeric",
            name="ck_market_offer_price",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_periods.id"), nullable=False, index=True
    )
    external_offer_id: Mapped[str | None] = mapped_column(String(24))
    external_order_id: Mapped[str | None] = mapped_column(String(24))
    side: Mapped[MarketOfferSide] = mapped_column(Enum(MarketOfferSide), nullable=False)
    publisher_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    counterparty_group_id: Mapped[int | None] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    strategy: Mapped[str | None] = mapped_column(String)
    estimated_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric())
    protected_quantity: Mapped[int | None] = mapped_column(Integer)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String)
    http_status: Mapped[int | None] = mapped_column(Integer)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    period: Mapped["BusinessPeriod"] = relationship()
    reservations: Mapped[list["MarketOfferReservation"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )


class MarketOfferReservation(Base):
    __tablename__ = "market_offer_reservations"
    __table_args__ = (
        UniqueConstraint("offer_id", "unit_id", name="uq_market_offer_unit"),
        Index(
            "uq_active_market_unit_reservation",
            "unit_id",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_offers.id"), nullable=False, index=True
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id"), nullable=False, index=True
    )
    source_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id")
    )
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    offer: Mapped[MarketOffer] = relationship(back_populates="reservations")
    unit: Mapped["Unit"] = relationship(back_populates="market_reservations")
