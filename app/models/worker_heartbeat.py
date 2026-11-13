from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkerHeartbeat(Base):
    """Durable evidence that a supervised worker is completing its cycles."""

    __tablename__ = "worker_heartbeats"

    period_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("business_periods.id"), primary_key=True
    )
    worker_name: Mapped[str] = mapped_column(String, primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    queue_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    oldest_queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
