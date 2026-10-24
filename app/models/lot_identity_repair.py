import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LotIdentityRepair(Base):
    """Keep the previous identity when official inventory resolves a fallback lot."""

    __tablename__ = "lot_identity_repairs"
    __table_args__ = (
        UniqueConstraint(
            "unit_id", "official_batch", name="uq_lot_identity_repairs_unit_batch"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"), nullable=False)
    previous_batch: Mapped[str] = mapped_column(String, nullable=False)
    official_batch: Mapped[str] = mapped_column(String, nullable=False)
    repaired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
