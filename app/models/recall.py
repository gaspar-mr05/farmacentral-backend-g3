import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.inbound_event import InboundEvent
    from app.models.location import Location
    from app.models.lot import Lot
    from app.models.order import Order
    from app.models.order_unit import OrderUnit
    from app.models.unit import Unit


class RecallStatus(StrEnum):
    RECEIVED = "received"
    INVESTIGATING = "investigating"
    RESPONDING = "responding"
    COMPLETED = "completed"
    FAILED = "failed"


class RecallNotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class Recall(Base, TimestampMixin):
    __tablename__ = "recalls"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(recall_id)) > 0", name="ck_recalls_recall_id_not_empty"
        ),
        CheckConstraint("length(btrim(batch)) > 0", name="ck_recalls_batch_not_empty"),
        CheckConstraint("length(btrim(sku)) > 0", name="ck_recalls_sku_not_empty"),
        CheckConstraint(
            "length(btrim(reason)) > 0", name="ck_recalls_reason_not_empty"
        ),
        CheckConstraint("deadline > issued_at", name="ck_recalls_deadline"),
        CheckConstraint(
            "last_report IS NULL OR jsonb_typeof(last_report) = 'object'",
            name="ck_recalls_last_report_object",
        ),
        CheckConstraint(
            "(reported_at IS NULL AND last_report IS NULL) OR "
            "(reported_at IS NOT NULL AND last_report IS NOT NULL)",
            name="ck_recalls_report_audit",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recall_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    inbound_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inbound_events.id"), unique=True, nullable=True
    )
    origin_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lots.id"), nullable=True, index=True
    )
    batch: Mapped[str] = mapped_column(String, nullable=False)
    sku: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[RecallStatus] = mapped_column(
        Enum(RecallStatus),
        default=RecallStatus.RECEIVED,
        server_default=text("'RECEIVED'"),
        nullable=False,
    )
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_report: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )

    inbound_event: Mapped["InboundEvent | None"] = relationship()
    origin_lot: Mapped["Lot | None"] = relationship()
    lots: Mapped[list["RecallLot"]] = relationship(
        back_populates="recall", cascade="all, delete-orphan"
    )
    units: Mapped[list["RecallUnit"]] = relationship(
        back_populates="recall", cascade="all, delete-orphan"
    )
    orders: Mapped[list["RecallOrder"]] = relationship(
        back_populates="recall", cascade="all, delete-orphan"
    )


class RecallLot(Base):
    __tablename__ = "recall_lots"
    __table_args__ = (
        UniqueConstraint("recall_id", "lot_id", name="uq_recall_lots_recall_lot"),
        UniqueConstraint("id", "recall_id", name="uq_recall_lots_id_recall"),
        ForeignKeyConstraint(
            ["parent_id", "recall_id"],
            ["recall_lots.id", "recall_lots.recall_id"],
            name="fk_recall_lots_parent_same_recall",
        ),
        CheckConstraint("generation >= 0", name="ck_recall_lots_generation"),
        CheckConstraint(
            "(generation = 0 AND parent_id IS NULL) OR "
            "(generation > 0 AND parent_id IS NOT NULL)",
            name="ck_recall_lots_parent_generation",
        ),
        Index("ix_recall_lots_recall_generation", "recall_id", "generation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recall_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recalls.id"), nullable=False
    )
    lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lots.id"), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    identified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    recall: Mapped["Recall"] = relationship(back_populates="lots")
    lot: Mapped["Lot"] = relationship()
    parent: Mapped["RecallLot | None"] = relationship(
        remote_side=[id, recall_id],
        back_populates="children",
        overlaps="lots,recall",
    )
    children: Mapped[list["RecallLot"]] = relationship(
        back_populates="parent", overlaps="lots,recall"
    )


class RecallUnit(Base):
    __tablename__ = "recall_units"
    __table_args__ = (
        UniqueConstraint("recall_id", "unit_id", name="uq_recall_units_recall_unit"),
        ForeignKeyConstraint(
            ["recall_id", "lot_id"],
            ["recall_lots.recall_id", "recall_lots.lot_id"],
            name="fk_recall_units_affected_lot",
        ),
        ForeignKeyConstraint(
            ["unit_id", "lot_id"],
            ["units.id", "units.lot_id"],
            name="fk_recall_units_unit_lot",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recall_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recalls.id"), nullable=False
    )
    lot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    original_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id"), nullable=False
    )
    identified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    quarantined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    recall: Mapped["Recall"] = relationship(back_populates="units")
    unit: Mapped["Unit"] = relationship()
    original_location: Mapped["Location"] = relationship()


class RecallOrder(Base):
    __tablename__ = "recall_orders"
    __table_args__ = (
        UniqueConstraint("recall_id", "order_id", name="uq_recall_orders_recall_order"),
        UniqueConstraint("id", "recall_id", name="uq_recall_orders_id_recall"),
        CheckConstraint(
            "length(btrim(customer_reference)) > 0",
            name="ck_recall_orders_customer_reference_not_empty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recall_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recalls.id"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), nullable=False)
    customer_reference: Mapped[str] = mapped_column(String, nullable=False)
    identified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    recall: Mapped["Recall"] = relationship(back_populates="orders")
    order: Mapped["Order"] = relationship()
    deliveries: Mapped[list["RecallDelivery"]] = relationship(
        back_populates="recall_order", cascade="all, delete-orphan"
    )
    notification: Mapped["RecallNotification | None"] = relationship(
        back_populates="recall_order", cascade="all, delete-orphan", uselist=False
    )


class RecallDelivery(Base):
    __tablename__ = "recall_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "recall_id", "order_unit_id", name="uq_recall_deliveries_assignment"
        ),
        UniqueConstraint("recall_id", "unit_id", name="uq_recall_deliveries_unit"),
        ForeignKeyConstraint(
            ["recall_order_id", "recall_id"],
            ["recall_orders.id", "recall_orders.recall_id"],
            name="fk_recall_deliveries_order_same_recall",
        ),
        ForeignKeyConstraint(
            ["recall_id", "lot_id"],
            ["recall_lots.recall_id", "recall_lots.lot_id"],
            name="fk_recall_deliveries_affected_lot",
        ),
        ForeignKeyConstraint(
            ["unit_id", "lot_id"],
            ["units.id", "units.lot_id"],
            name="fk_recall_deliveries_unit_lot",
        ),
        ForeignKeyConstraint(
            ["order_unit_id", "unit_id"],
            ["order_units.id", "order_units.unit_id"],
            name="fk_recall_deliveries_order_unit",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recall_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recalls.id"), nullable=False
    )
    recall_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    lot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    order_unit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    recall_order: Mapped["RecallOrder"] = relationship(back_populates="deliveries")
    unit: Mapped["Unit"] = relationship()
    order_unit: Mapped["OrderUnit"] = relationship(overlaps="unit")


class RecallNotification(Base):
    __tablename__ = "recall_notifications"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_recall_notifications_payload_object",
        ),
        CheckConstraint(
            "response IS NULL OR jsonb_typeof(response) = 'object'",
            name="ck_recall_notifications_response_object",
        ),
        CheckConstraint(
            "(status = 'SENT' AND sent_at IS NOT NULL) OR "
            "(status != 'SENT' AND sent_at IS NULL)",
            name="ck_recall_notifications_sent_at",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_recall_notifications_attempt_count"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recall_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recall_orders.id"), unique=True, nullable=False
    )
    status: Mapped[RecallNotificationStatus] = mapped_column(
        Enum(RecallNotificationStatus),
        default=RecallNotificationStatus.PENDING,
        server_default=text("'PENDING'"),
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    response: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    recall_order: Mapped["RecallOrder"] = relationship(back_populates="notification")
