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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.inbound_event import InboundEvent


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessagingMessage(Base):
    """One authentic message in a scoped phone conversation."""

    __tablename__ = "messaging_messages"
    __table_args__ = (
        UniqueConstraint(
            "direction",
            "external_message_id",
            name="uq_messaging_message_external_id",
        ),
        CheckConstraint(
            "length(btrim(phone_normalized)) > 0",
            name="ck_messaging_message_phone",
        ),
        CheckConstraint("group_id > 0", name="ck_messaging_message_group"),
        CheckConstraint(
            "length(btrim(environment)) > 0",
            name="ck_messaging_message_environment",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_messaging_message_payload",
        ),
        Index(
            "ix_messaging_history_scope",
            "group_id",
            "environment",
            "period_id",
            "phone_normalized",
            "occurred_at",
        ),
        Index("ix_messaging_phone_time", "phone_normalized", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    inbound_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inbound_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection), nullable=False
    )
    external_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    phone_normalized: Mapped[str] = mapped_column(String(16), nullable=False)
    sender: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    period_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("business_periods.id"), nullable=True
    )

    inbound_event: Mapped["InboundEvent"] = relationship(
        back_populates="conversation_messages"
    )
