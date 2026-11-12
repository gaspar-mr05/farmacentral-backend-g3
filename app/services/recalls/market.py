"""Release affected market custody before any physical quarantine movement."""

from sqlalchemy import func, select

from app.models import (
    CustodyEvent,
    MarketOffer,
    MarketOfferReservation,
    MarketOfferSide,
    Unit,
)
from app.services.trades.custody import TradeCustodyService


class RecallMarketPendingError(Exception):
    pass


def affected_offers(session, lot_ids):
    return session.scalars(
        select(MarketOffer)
        .join(MarketOfferReservation)
        .join(Unit)
        .where(
            Unit.lot_id.in_(lot_ids),
            MarketOfferReservation.released_at.is_(None),
        )
        .distinct()
        .order_by(MarketOffer.id)
    ).all()


async def release_market_custody(
    session, inventory, trades, orders, offers, check_deadline
):
    for candidate in offers:
        check_deadline()
        offer = session.scalar(
            select(MarketOffer)
            .where(MarketOffer.id == candidate.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        custody = TradeCustodyService(
            session,
            farma_central=inventory,
            trades=trades,
            purchase_orders=orders,
            period=offer.period,
        )
        if not offer.external_offer_id:
            # No POST could have run in these pre-publication states. Otherwise
            # absence of an ID is not evidence that publication failed.
            if offer.status not in {"preparing", "movement_uncertain", "failed"}:
                raise RecallMarketPendingError(
                    "Affected offer has no official ID; reconcile publication"
                )
            custody._release(offer, status="failed")
            session.commit()
            continue
        official = await trades.get_offer(offer.external_offer_id)
        if official.status == "open":
            if (
                offer.side != MarketOfferSide.SELL
                or offer.publisher_group_id != offer.period.group_id
            ):
                raise RecallMarketPendingError(
                    "Cannot cancel another group's open offer"
                )
            # This commit survives a timeout. A subsequent attempt starts with
            # GET; even a successful cancel response is followed by a fresh GET.
            offer.status = "recall_cancel_pending"
            session.commit()
            check_deadline()
            await trades.cancel_offer(offer.external_offer_id)
            official = await trades.get_offer(offer.external_offer_id)
        check_deadline()
        trade = await custody.reconcile_offer(official)
        if official.status not in {"cancelled", "expired", "closed"}:
            raise RecallMarketPendingError("Affected market offer is still open")
        if trade is not None:
            count = session.scalar(
                select(func.count())
                .select_from(CustodyEvent)
                .where(CustodyEvent.market_trade_id == trade.id)
            )
            if count != trade.quantity:
                raise RecallMarketPendingError(
                    "Market close won cancellation; "
                    "official transferred unit IDs are required"
                )
            # record_trade_units normally released these reservations already.
            if any(r.released_at is None for r in offer.reservations):
                raise RecallMarketPendingError(
                    "Closed offer still has unresolved unit reservations"
                )
