import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.market_trade import MarketTrade
    from app.models.order import Order


class InvoiceSide(StrEnum):
    CLIENT = "client"  # The group owes this invoice.
    SUPPLIER = "supplier"  # The group issued this invoice.


class InvoiceStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"


class BillingTaskKind(StrEnum):
    EMIT_INVOICE = "emit_invoice"
    PAY_INVOICE = "pay_invoice"


class BillingOperationStatus(StrEnum):
    READY = "ready"
    IN_FLIGHT = "in_flight"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("id", "period_id", name="uq_invoices_id_period"),
        UniqueConstraint(
            "period_id", "external_invoice_id", name="uq_invoice_external_id"
        ),
        UniqueConstraint(
            "period_id", "external_order_id", name="uq_invoice_external_order"
        ),
        UniqueConstraint("period_id", "sale_order_id", name="uq_invoice_sale_order"),
        ForeignKeyConstraint(
            ["market_trade_id", "period_id"],
            ["market_trades.id", "market_trades.period_id"],
            name="fk_invoice_trade_period",
        ),
        CheckConstraint(
            "length(btrim(external_invoice_id)) > 0", name="ck_invoice_external_id"
        ),
        CheckConstraint(
            "length(btrim(counterparty_id)) > 0", name="ck_invoice_counterparty"
        ),
        CheckConstraint("length(btrim(channel)) > 0", name="ck_invoice_channel"),
        CheckConstraint(
            "external_order_id IS NULL OR length(btrim(external_order_id)) > 0",
            name="ck_invoice_external_order",
        ),
        CheckConstraint(
            "principal >= 0 AND principal < 'Infinity'::numeric",
            name="ck_invoice_principal",
        ),
        CheckConstraint(
            "interest >= 0 AND interest < 'Infinity'::numeric",
            name="ck_invoice_interest",
        ),
        CheckConstraint(
            "total >= 0 AND total < 'Infinity'::numeric", name="ck_invoice_total"
        ),
        CheckConstraint(
            "sale_order_id IS NULL OR "
            "(side = 'SUPPLIER' AND external_order_id IS NOT NULL)",
            name="ck_invoice_sale_role",
        ),
        Index("ix_invoices_pending", "period_id", "side", "status", "issued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_periods.id"), nullable=False
    )
    external_invoice_id: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[InvoiceSide] = mapped_column(Enum(InvoiceSide), nullable=False)
    counterparty_id: Mapped[str] = mapped_column(String, nullable=False)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), nullable=False)
    principal: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    interest: Mapped[Decimal] = mapped_column(
        Numeric(), nullable=False, default=Decimal(0), server_default=text("0")
    )
    total: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    external_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_order_id: Mapped[str | None] = mapped_column(String)
    sale_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    market_trade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    sale_order: Mapped["Order | None"] = relationship()
    market_trade: Mapped["MarketTrade | None"] = relationship(
        foreign_keys=[market_trade_id]
    )


class BillingTask(Base, TimestampMixin):
    __tablename__ = "billing_tasks"
    __table_args__ = (
        UniqueConstraint(
            "period_id", "kind", "external_order_id", name="uq_billing_task_order"
        ),
        UniqueConstraint(
            "period_id", "kind", "invoice_id", name="uq_billing_task_invoice"
        ),
        ForeignKeyConstraint(
            ["invoice_id", "period_id"],
            ["invoices.id", "invoices.period_id"],
            name="fk_billing_task_invoice_period",
        ),
        CheckConstraint(
            "(kind = 'EMIT_INVOICE' AND external_order_id IS NOT NULL "
            "AND length(btrim(external_order_id)) > 0) OR "
            "(kind = 'PAY_INVOICE' AND invoice_id IS NOT NULL "
            "AND external_order_id IS NULL)",
            name="ck_billing_task_target",
        ),
        CheckConstraint(
            "(status = 'CONFIRMED' AND confirmed_at IS NOT NULL "
            "AND invoice_id IS NOT NULL) OR "
            "(status != 'CONFIRMED' AND confirmed_at IS NULL)",
            name="ck_billing_task_confirmation",
        ),
        Index("ix_billing_tasks_ready", "period_id", "status", "next_retry_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("business_periods.id"), nullable=False
    )
    kind: Mapped[BillingTaskKind] = mapped_column(Enum(BillingTaskKind), nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(String)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[BillingOperationStatus] = mapped_column(
        Enum(BillingOperationStatus),
        nullable=False,
        default=BillingOperationStatus.READY,
        server_default=text("'READY'"),
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    invoice: Mapped["Invoice | None"] = relationship(foreign_keys=[invoice_id])
    attempts: Mapped[list["BillingAttempt"]] = relationship(
        back_populates="task", order_by="BillingAttempt.number"
    )


class BillingAttempt(Base):
    __tablename__ = "billing_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "number", name="uq_billing_attempt_number"),
        CheckConstraint("number > 0", name="ck_billing_attempt_number"),
        CheckConstraint(
            "(status = 'IN_FLIGHT' AND finished_at IS NULL) OR "
            "(status IN ('CONFIRMED', 'UNCERTAIN', 'FAILED') "
            "AND finished_at IS NOT NULL)",
            name="ck_billing_attempt_result",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_billing_attempt_dates",
        ),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_billing_attempt_http_status",
        ),
        CheckConstraint(
            "response IS NULL OR jsonb_typeof(response) = 'object'",
            name="ck_billing_attempt_response",
        ),
        Index(
            "uq_billing_attempt_in_flight",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'IN_FLIGHT'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("billing_tasks.id"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[BillingOperationStatus] = mapped_column(
        Enum(BillingOperationStatus),
        nullable=False,
        default=BillingOperationStatus.IN_FLIGHT,
        server_default=text("'IN_FLIGHT'"),
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))

    task: Mapped["BillingTask"] = relationship(back_populates="attempts")
