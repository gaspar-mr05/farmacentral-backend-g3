"""A bounded working envelope; estimates never alter invoiced business metrics."""

from decimal import Decimal

from sqlalchemy import and_, or_, select

from app.models import (
    MarketOffer,
    MarketOfferSide,
    ReplenishmentState,
)
from app.services.billing.metrics import BusinessMetricsService


def spending_room(session, period, settings, catalog, prices):
    metrics = BusinessMetricsService(session, period).calculate()
    initial = settings.replenishment_spending_limit
    if initial is None:
        initial = sum(
            (
                prices.get(p.sku, Decimal(0))
                * min(200, settings.replenishment_target_stock)
                for p in catalog.values()
                if p.sellable
            ),
            Decimal(0),
        )
    # Only actually invoiced positive margin increases the initial envelope.
    # Planned orders and leftover inventory are never treated as income.
    envelope = initial + max(Decimal(0), metrics.margin)
    spent = Decimal(0)
    pending = Decimal(0)
    for row in session.scalars(
        select(ReplenishmentState).where(
            ReplenishmentState.period_id == period.id,
        )
    ):
        spent += row.estimated_spend
        operation = row.pending_operation or {}
        if operation:
            pending += Decimal(operation["cost"])
    for offer in session.scalars(
        select(MarketOffer).where(
            MarketOffer.period_id == period.id,
            MarketOffer.reconciled_at.is_(None),
            MarketOffer.status.not_in(["cancelled", "expired", "failed"]),
            or_(
                and_(
                    MarketOffer.side == MarketOfferSide.BUY,
                    MarketOffer.publisher_group_id == period.group_id,
                ),
                and_(
                    MarketOffer.side == MarketOfferSide.SELL,
                    MarketOffer.publisher_group_id != period.group_id,
                ),
            ),
        )
    ):
        pending += offer.quantity * offer.unit_price
    # Apply the accrued-loss guard separately: subtracting both estimated spend
    # and the invoices for those same purchases would charge the lot twice.
    room = max(
        Decimal(0),
        min(
            envelope - spent - pending,
            initial + metrics.margin - pending,
        ),
    )
    # SOAP balance is net paid invoices, not a funded wallet/credit limit.
    # Zero is normal before the first payment; using it as cash capacity would
    # prevent startup. The explicit envelope and accrued-loss guard bound risk.
    return room, {
        "period_limit": str(envelope),
        "period_spent": str(spent),
        "pending_outlay": str(pending),
        "invoiced_margin": str(metrics.margin),
        "accounting_balance": str(metrics.bank_balance)
        if metrics.bank_balance is not None
        else None,
        "remaining": str(room),
    }
