import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
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

if TYPE_CHECKING:
    from app.models.order import Order


class InboundEventSource(StrEnum):
    SFTP = "sftp"
    PUBSUB = "pubsub"
    MESSAGING = "messaging"
    RECALL = "recall"


class InboundEventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class MessagingDecision(StrEnum):
    ORDER = "order"
    DISCARDED = "discarded"


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint(
            "source", "external_event_id", name="uq_inbound_events_source_external_id"
        ),
        CheckConstraint(
            "length(btrim(external_event_id)) > 0",
            name="ck_inbound_events_external_id_not_empty",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_inbound_events_attempt_count"),
        CheckConstraint(
            "interpreted_quantity IS NULL OR interpreted_quantity > 0",
            name="ck_inbound_events_interpreted_quantity",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_inbound_events_payload_object"
        ),
        Index("ix_inbound_events_pending", "status", "next_retry_at", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[InboundEventSource] = mapped_column(
        Enum(InboundEventSource), nullable=False
    )
    external_event_id: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[InboundEventStatus] = mapped_column(
        Enum(InboundEventStatus),
        default=InboundEventStatus.PENDING,
        server_default=text("'PENDING'"),
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id"), nullable=True, index=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_output: Mapped[Any | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    interpreted_sku: Mapped[str | None] = mapped_column(String, nullable=True)
    interpreted_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interpreted_delivery_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision: Mapped[MessagingDecision | None] = mapped_column(
        Enum(MessagingDecision), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_order_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    order: Mapped["Order | None"] = relationship(back_populates="inbound_events")
