import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BillingSyncState(Base):
    """Durable progress for four invoice streams and the bank observation."""

    __tablename__ = "billing_sync_states"

    period_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("business_periods.id"), primary_key=True
    )
    stream: Mapped[str] = mapped_column(String, primary_key=True)
    cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    full_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    full_target: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    full_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    full_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_full_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)
    http_status: Mapped[int | None] = mapped_column(Integer)
    balance: Mapped[Decimal | None] = mapped_column(Numeric())
