# app/models/custody_event.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CustodyEventType(str, enum.Enum):
    RECEIVED = "received"
    MOVED = "moved"
    # a futuro: PRODUCTION_CONSUMED, PRODUCED, SOLD, DISPATCHED — se agregan
    # solo cuando esas features realmente existan (Pasos 12, 18, 19)


class CustodyEvent(Base):
    __tablename__ = "custody_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"), nullable=False, index=True)
    event_type: Mapped[CustodyEventType] = mapped_column(Enum(CustodyEventType), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    from_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id"), nullable=True
    )
    to_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id"), nullable=True
    )

    # Referencias opcionales a futuro (Paso 11+ y Paso 16+). Nullable a propósito:
    # no forzamos estas FKs hasta que ProductionRun y Order existan de verdad.
    production_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    unit: Mapped["Unit"] = relationship(back_populates="custody_events")
    from_location: Mapped["Location | None"] = relationship(foreign_keys=[from_location_id])
    to_location: Mapped["Location | None"] = relationship(foreign_keys=[to_location_id])