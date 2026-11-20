"""Confirmed delivered purchases whose remaining work is historical unit tracing."""

from sqlalchemy import select

from app.models import MarketOffer, MarketRole, MarketTrade, ProcurementRequest


def accounted_deliveries(period_id):
    return (
        select(MarketOffer.id)
        .join(ProcurementRequest, ProcurementRequest.market_offer_id == MarketOffer.id)
        .join(
            MarketTrade,
            (MarketTrade.period_id == MarketOffer.period_id)
            & (MarketTrade.external_offer_id == MarketOffer.external_offer_id),
        )
        .where(
            MarketOffer.period_id == period_id,
            ProcurementRequest.period_id == period_id,
            MarketOffer.status == "closed",
            MarketTrade.role == MarketRole.BUYER,
            ProcurementRequest.decision["replenishment_accounted"]
            .as_boolean()
            .is_(True),
        )
    )
