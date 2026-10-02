import uuid
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.order import Order


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    CANCELLED = "cancelled"
    ERROR = "error"
    OBSOLETE = "obsolete"


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id"), nullable=False, index=True
    )
    external_transaction_id: Mapped[str] = mapped_column(
        String, unique=True, nullable=False
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="payments")
