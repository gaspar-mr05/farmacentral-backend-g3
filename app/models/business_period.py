import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class BusinessPeriod(Base, TimestampMixin):
    """Explicit accounting scope; creating it does not activate any worker."""

    __tablename__ = "business_periods"
    __table_args__ = (
        UniqueConstraint("environment", "code", "group_id", name="uq_business_period"),
        CheckConstraint("length(btrim(environment)) > 0", name="ck_period_environment"),
        CheckConstraint("length(btrim(code)) > 0", name="ck_period_code"),
        CheckConstraint("group_id > 0", name="ck_period_group"),
        CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at", name="ck_period_dates"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    environment: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, nullable=False)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
