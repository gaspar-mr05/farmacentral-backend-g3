import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.inbound_event import InboundEvent
    from app.models.order_unit import OrderUnit
    from app.models.payment import Payment


class OrderStatus(StrEnum):
    RECEIVED = "received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    PAYMENT_ERROR = "payment_error"


class OrderSource(StrEnum):
    WEB = "web"
    SFTP = "sftp"
    PUBSUB = "pubsub"
    MESSAGING = "messaging"


class ExternalOrderStatus(StrEnum):
    CREATED = "creada"
    ACCEPTED = "aceptada"
    REJECTED = "rechazada"
    CANCELLED = "anulada"
    EXPIRED = "vencida"
    FULFILLED = "cumplida"


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("external_order_id", name="uq_orders_external_order_id"),
        CheckConstraint(
            "external_order_id IS NULL OR external_order_id ~ '^[0-9a-f]{24}$'",
            name="ck_orders_external_order_id",
        ),
        CheckConstraint(
            "source != 'WEB' OR buyer_email IS NOT NULL",
            name="ck_orders_web_buyer_email",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    buyer_name: Mapped[str] = mapped_column(String, nullable=False)
    buyer_email: Mapped[str | None] = mapped_column(String, nullable=True)
    external_order_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    external_customer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    external_status: Mapped[ExternalOrderStatus | None] = mapped_column(
        Enum(ExternalOrderStatus), nullable=True
    )
    source: Mapped[OrderSource] = mapped_column(Enum(OrderSource), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list["Payment"]] = relationship(back_populates="order")
    inbound_events: Mapped[list["InboundEvent"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    assigned_units: Mapped[list["OrderUnit"]] = relationship(
        back_populates="order_item", cascade="all, delete-orphan"
    )

    @property
    def line_total(self) -> int:
        return self.quantity * self.unit_price
