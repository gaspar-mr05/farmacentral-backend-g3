"""Resume confirmed supply without requiring old stock to remain untouched."""

from sqlalchemy import and_, func, or_, select

from app.models import (
    CustodyEvent,
    CustodyEventType,
    Lot,
    MarketOffer,
    MarketOfferSide,
    MarketRole,
    MarketTrade,
    Product,
    ReplenishmentState,
    Unit,
)


def _received(session, sku):
    return session.scalar(
        select(func.count(func.distinct(CustodyEvent.unit_id)))
        .join(Unit, CustodyEvent.unit_id == Unit.id)
        .join(Lot, Unit.lot_id == Lot.id)
        .join(Product, Lot.product_id == Product.id)
        .where(Product.sku == sku, CustodyEvent.event_type == CustodyEventType.RECEIVED)
    )


def _market_receipts(session, period_id, sku):
    return session.scalar(
        select(func.count())
        .select_from(MarketTrade)
        .where(
            MarketTrade.period_id == period_id,
            MarketTrade.sku == sku,
            MarketTrade.role == MarketRole.BUYER,
        )
    )


def _incoming_market(session, period, sku):
    return (
        session.scalar(
            select(MarketOffer.id)
            .where(
                MarketOffer.period_id == period.id,
                MarketOffer.sku == sku,
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
            .limit(1)
        )
        is not None
    )


def _other_supply(session, period, sku, key=None):
    query = select(ReplenishmentState.id).where(
        ReplenishmentState.period_id == period.id,
        ReplenishmentState.pending_operation["kind"].astext == "supply",
        ReplenishmentState.pending_operation["sku"].astext == sku,
    )
    if key is not None:
        pending_key = ReplenishmentState.pending_operation["key"].astext
        query = query.where(or_(pending_key.is_(None), pending_key != key))
    return session.scalar(query.limit(1)) is not None


def supply_receipt_baseline(session, period, sku, *, inventory_complete):
    # An incomplete snapshot can discover older units later. Do not treat those
    # discoveries, or a concurrent market purchase, as this supply's delivery.
    if (
        not inventory_complete
        or _incoming_market(session, period, sku)
        or _other_supply(session, period, sku)
    ):
        return {}
    return {
        "received_before": _received(session, sku),
        "market_receipts_before": _market_receipts(session, period.id, sku),
    }


def supply_resume_basis(session, period, operation, *, usable_count):
    # Legacy operations lack a receipt baseline. They may still proceed using
    # genuinely usable stock, without claiming which delivery supplied it.
    needed = operation.get("needed", operation["quantity"])
    if usable_count >= needed:
        return "usable_stock"
    sku = operation["sku"]
    if (
        "received_before" in operation
        and not _incoming_market(session, period, sku)
        and not _other_supply(session, period, sku, operation["key"])
        and _market_receipts(session, period.id, sku)
        == operation["market_receipts_before"]
        and _received(session, sku) - operation["received_before"]
        == operation["quantity"]
    ):
        # RECEIVED events survive consumption, dispatch, expiry and quarantine.
        # This acknowledges receipt only; it never makes these units available.
        return "receipt_ledger"
    return None
