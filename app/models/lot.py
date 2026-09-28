# app/models/lot.py
import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.unit import Unit


class LotOrigin(StrEnum):
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
