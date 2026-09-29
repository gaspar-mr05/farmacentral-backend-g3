# app/models/production_run.py
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ProductionRun(Base, TimestampMixin):
    __tablename__ = "production_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # El lote generado por esta producción. Nullable porque, según el flujo del
    # Paso 12, la producción se solicita antes de que el lote de salida exista
    # (Farma Central entrega la hora de recepción, no el lote inmediatamente).
    output_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lots.id"), unique=True, nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    output_lot: Mapped["Lot | None"] = relationship(
        back_populates="produced_by", foreign_keys=[output_lot_id]
    )
    inputs: Mapped[list["ProductionInput"]] = relationship(
        back_populates="production_run", cascade="all, delete-orphan"
    )