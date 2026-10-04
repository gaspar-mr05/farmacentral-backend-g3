import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.lot import Lot


class ProductCategory(StrEnum):
    INSUMO = "insumo"
    ACONDICIONADO = "acondicionado"
    KIT = "kit"


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sku: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[ProductCategory] = mapped_column(
        Enum(ProductCategory), nullable=False
    )
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_refrigeration: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    lots: Mapped[list["Lot"]] = relationship(back_populates="product")
