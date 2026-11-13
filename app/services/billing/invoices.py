import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clients.billing import BillingInvalidResponseError
from app.models import Invoice, InvoiceSide, InvoiceStatus, MarketRole, MarketTrade

from .queue import enqueue_payment


def _link_explicit_market_order(session, period, row, invoice, side):
    """Use a unique official description reference; never infer by amount alone."""
    if invoice.channel.lower() not in {
        "market",
        "mercado",
    } or row.channel.lower() not in {"market", "mercado"}:
        return
    references = {
        value.lower()
        for value in re.findall(
            r"\border\s+([0-9a-f]{24})(?![0-9a-f])", invoice.description, re.IGNORECASE
        )
    }
    if len(references) != 1:
        return
    identity = references.pop()
    trade = session.scalar(
        select(MarketTrade).where(
            MarketTrade.period_id == period.id,
            MarketTrade.external_order_id == identity,
        )
    )
    if trade is None:
        return
    role = MarketRole.BUYER if side == InvoiceSide.CLIENT else MarketRole.SELLER
    supplier = (
        trade.counterparty_id if role == MarketRole.BUYER else str(period.group_id)
    )
    client = str(period.group_id) if role == MarketRole.BUYER else trade.counterparty_id
    expected = trade.quantity * trade.unit_price
    if (
        trade.role != role
        or invoice.supplier_id != supplier
        or invoice.client_id != client
        or invoice.principal != expected
        or row.principal != expected
        or invoice.issued_at < trade.closed_at
    ):
        raise BillingInvalidResponseError(
            "Explicit market invoice reference conflicts with trade"
        )
    if row.external_order_id not in {None, identity} or row.market_trade_id not in {
        None,
        trade.id,
    }:
        raise BillingInvalidResponseError(
            "Invoice already has a different verified association"
        )
    other = session.scalar(
        select(Invoice.id).where(
            Invoice.period_id == period.id,
            Invoice.external_order_id == identity,
            Invoice.id != row.id,
        )
    )
    if other is not None:
        raise BillingInvalidResponseError(
            "Another invoice already references this market order"
        )
    row.external_order_id = identity
    row.market_trade_id = trade.id


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
    row = session.scalars(
        select(Invoice)
        .where(
            Invoice.period_id == period.id,
            Invoice.external_invoice_id == invoice.invoice_id,
        )
        .execution_options(populate_existing=True)
    ).one()
    _link_explicit_market_order(session, period, row, invoice, side)
    if row.side == InvoiceSide.CLIENT and row.status == InvoiceStatus.PENDING:
        enqueue_payment(session, row)
    return row
