import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ReplenishmentState(Base, TimestampMixin):
    __tablename__ = "replenishment_states"
    __table_args__ = (
        UniqueConstraint("period_id", "sku", name="uq_replenishment_sku"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_periods.id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(String, nullable=False)
    minimum: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    goal: Mapped[dict | None] = mapped_column(JSONB)
    pending_operation: Mapped[dict | None] = mapped_column(JSONB)
    procurement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("procurement_requests.id")
    )
    estimated_spend: Mapped[Decimal] = mapped_column(
        Numeric(), nullable=False, default=Decimal(0), server_default="0"
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String)
