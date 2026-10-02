# app/models/production_input.py
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.lot import Lot
    from app.models.production_input_unit import ProductionInputUnit
    from app.models.production_run import ProductionRun


class ProductionInput(Base):
    __tablename__ = "production_inputs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    production_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("production_runs.id"), nullable=False, index=True
    )
    input_lot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lots.id"), nullable=False, index=True
    )
    quantity_consumed: Mapped[int] = mapped_column(Integer, nullable=False)

    production_run: Mapped["ProductionRun"] = relationship(back_populates="inputs")
    input_lot: Mapped["Lot"] = relationship(back_populates="consumed_in")
    units: Mapped[list["ProductionInputUnit"]] = relationship(
        back_populates="production_input", cascade="all, delete-orphan"
    )
