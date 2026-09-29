# app/models/production_input_unit.py
import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ProductionInputUnit(Base):
    __tablename__ = "production_input_units"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    production_input_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("production_inputs.id"), nullable=False, index=True
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id"), unique=True, nullable=False
    )

    production_input: Mapped["ProductionInput"] = relationship(back_populates="units")
    unit: Mapped["Unit"] = relationship()