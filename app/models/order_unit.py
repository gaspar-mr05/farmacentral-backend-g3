import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.order import OrderItem
    from app.models.unit import Unit


class OrderUnit(Base):
    __tablename__ = "order_units"
    __table_args__ = (UniqueConstraint("unit_id", name="uq_order_units_unit_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("order_items.id"), nullable=False, index=True
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("units.id"), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    order_item: Mapped["OrderItem"] = relationship(back_populates="assigned_units")
    unit: Mapped["Unit"] = relationship(back_populates="order_assignment")

    @property
    def external_unit_id(self) -> str:
        return self.unit.external_unit_id

    @property
    def lot_id(self) -> uuid.UUID:
        return self.unit.lot_id
