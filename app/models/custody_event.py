import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.location import Location
    from app.models.unit import Unit


class CustodyEventType(StrEnum):
    RECEIVED = "received"
    MOVED = "moved"
    PRODUCTION_CONSUMED = "production_consumed"
    PRODUCED = "produced"
    DISPATCHED = "dispatched"


class CustodyEvent(Base):
    __tablename__ = "custody_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id"), nullable=False, index=True
    )
    event_type: Mapped[CustodyEventType] = mapped_column(
        Enum(CustodyEventType), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    from_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id"), nullable=True
    )
    to_location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id"), nullable=True
    )

    # Referencias opcionales a los procesos que originan el evento.
    production_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id"), nullable=True
    )

    unit: Mapped["Unit"] = relationship(back_populates="custody_events")
    from_location: Mapped["Location | None"] = relationship(
        foreign_keys=[from_location_id]
    )
    to_location: Mapped["Location | None"] = relationship(foreign_keys=[to_location_id])
