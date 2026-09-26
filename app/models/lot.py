# app/models/lot.py
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin


class LotOrigin(str, enum.Enum):
    OWN_PRODUCTION = "own_production"
    FARMA_CENTRAL = "farma_central"
    OTHER_DISTRIBUTOR = "other_distributor"


class Lot(Base, TimestampMixin):
    __tablename__ = "lots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_lot_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id"), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    origin: Mapped[LotOrigin] = mapped_column(Enum(LotOrigin), nullable=False)

    product: Mapped["Product"] = relationship(back_populates="lots")
    units: Mapped[list["Unit"]] = relationship(back_populates="lot")
