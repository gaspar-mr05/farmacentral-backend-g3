# app/models/unit.py
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.custody_event import CustodyEvent
    from app.models.location import Location
    from app.models.lot import Lot


class Unit(Base, TimestampMixin):
    __tablename__ = "units"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_unit_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    lot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("lots.id"), nullable=False)
    current_location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, default="available", nullable=False)

    lot: Mapped["Lot"] = relationship(back_populates="units")
    current_location: Mapped["Location"] = relationship(back_populates="units")
    custody_events: Mapped[list["CustodyEvent"]] = relationship(back_populates="unit")
