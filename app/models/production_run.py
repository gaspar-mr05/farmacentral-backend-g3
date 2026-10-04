import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.lot import Lot
    from app.models.production_input import ProductionInput


class ProductionRun(Base, TimestampMixin):
    __tablename__ = "production_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    output_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lots.id"), unique=True, nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    output_lot: Mapped["Lot | None"] = relationship(
        back_populates="produced_by", foreign_keys=[output_lot_id]
    )
    inputs: Mapped[list["ProductionInput"]] = relationship(
        back_populates="production_run", cascade="all, delete-orphan"
    )
    expected_sku: Mapped[str] = mapped_column(String, nullable=False)
    expected_quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
