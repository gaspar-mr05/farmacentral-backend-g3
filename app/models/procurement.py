import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ProcurementRequest(Base, TimestampMixin):
    """One durable sourcing decision; an open bid is never confirmed capacity."""

    __tablename__ = "procurement_requests"
    __table_args__ = (
        UniqueConstraint("period_id", "request_key", name="uq_procurement_request"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_periods.id"), nullable=False
    )
    request_key: Mapped[str] = mapped_column(String, nullable=False)
    demand: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decision: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, nullable=False)
    market_offer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_offers.id")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_error: Mapped[str | None] = mapped_column(String)
