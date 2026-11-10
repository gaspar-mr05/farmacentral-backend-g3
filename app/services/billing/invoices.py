from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clients.billing import BillingInvalidResponseError
from app.models import Invoice, InvoiceSide, InvoiceStatus


def upsert_invoice(session: Session, period, invoice, side, now) -> Invoice:
    existing = session.scalar(
        select(Invoice).where(
            Invoice.period_id == period.id,
            Invoice.external_invoice_id == invoice.invoice_id,
        )
    )
    counterparty = (
        invoice.supplier_id if side == InvoiceSide.CLIENT else invoice.client_id
    )
    if existing and (
        existing.side != side
        or existing.counterparty_id != counterparty
        or existing.issued_at != invoice.issued_at
    ):
        raise BillingInvalidResponseError("Invoice identity changed")
    values = dict(
        period_id=period.id,
        external_invoice_id=invoice.invoice_id,
        side=side,
        counterparty_id=counterparty,
        channel=invoice.channel,
        status=invoice.status,
        principal=invoice.principal,
        interest=invoice.interest,
        total=invoice.total,
        issued_at=invoice.issued_at,
        external_updated_at=invoice.updated_at,
        observed_at=now,
        paid_at=invoice.updated_at if invoice.status == InvoiceStatus.PAID else None,
    )
    statement = insert(Invoice).values(**values)
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_invoice_external_id",
            set_={
                "updated_at": now,
                **{
                    name: getattr(statement.excluded, name)
                    for name in (
                        "status",
                        "principal",
                        "interest",
                        "total",
                        "external_updated_at",
                        "observed_at",
                        "paid_at",
                    )
                },
            },
            where=(
                Invoice.external_updated_at <= statement.excluded.external_updated_at
            )
            & (
                (Invoice.status == InvoiceStatus.PENDING)
                | (statement.excluded.status == InvoiceStatus.PAID)
            ),
        )
    )
    return session.scalars(
        select(Invoice)
        .where(
            Invoice.period_id == period.id,
            Invoice.external_invoice_id == invoice.invoice_id,
        )
        .execution_options(populate_existing=True)
    ).one()
