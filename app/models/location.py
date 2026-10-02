# app/models/location.py
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.unit import Unit


class Location(Base, TimestampMixin):
    __tablename__ = "locations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_refrigerated: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_sellable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    units: Mapped[list["Unit"]] = relationship(back_populates="current_location")
