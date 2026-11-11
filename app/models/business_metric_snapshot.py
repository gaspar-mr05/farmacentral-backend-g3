import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BusinessMetricSnapshot(Base):
    """Point-in-time business metrics for one accounting period."""

    __tablename__ = "business_metric_snapshots"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(commit_sha)) > 0", name="ck_metric_snapshot_commit"
        ),
        CheckConstraint(
            "jsonb_typeof(metrics) = 'object'", name="ck_metric_snapshot_metrics"
        ),
        Index("ix_metric_snapshots_period_time", "period_id", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_periods.id"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    is_closing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
