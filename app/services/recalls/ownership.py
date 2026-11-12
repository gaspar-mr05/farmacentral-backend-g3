"""Negative ownership needs a complete inventory read and retained history."""

from datetime import UTC, datetime

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.models import (
    CustodyEvent,
    CustodyEventType,
    Lot,
    MarketOffer,
    MarketTrade,
    ProductionRun,
    Unit,
)
from app.queries.recalls import get_origin_lot
from app.services.inventory.sync import InventorySyncService

from .investigation import RecallInvestigationError


async def verify_absence(client, session, batch):
    try:
        result = await InventorySyncService(client, session).synchronize()
    except SQLAlchemyError:
        raise
    except Exception as exc:
        raise RecallInvestigationError(
            "Origin batch is not synchronized; could not verify complete inventory"
        ) from exc
    if get_origin_lot(session, batch) is not None:
        return None
    if not result.inventory_complete:
        raise RecallInvestigationError(
            "Origin batch is not synchronized; inventory listing is incomplete"
        )
    provisional = session.scalar(
        select(Unit.id)
        .join(Lot)
        .where(
            or_(
                Lot.external_lot_id.startswith("unreported:"),
                Lot.external_lot_id.startswith("production:"),
            )
        )
        .limit(1)
    )
    missing_custody = session.scalar(
        select(Unit.id)
        .where(
            ~exists().where(
                CustodyEvent.unit_id == Unit.id,
                CustodyEvent.event_type.in_(
                    [CustodyEventType.RECEIVED, CustodyEventType.PRODUCED]
                ),
            )
        )
        .limit(1)
    )
    pending_production = session.scalar(
        select(ProductionRun.id)
        .where(
            or_(
                ProductionRun.completed_at.is_(None),
                ProductionRun.output_lot_id.is_(None),
            )
        )
        .limit(1)
    )
    count = (
        select(func.count(CustodyEvent.id))
        .where(CustodyEvent.market_trade_id == MarketTrade.id)
        .correlate(MarketTrade)
        .scalar_subquery()
    )
    missing_trade_units = session.scalar(
        select(MarketTrade.id).where(count != MarketTrade.quantity).limit(1)
    )
    uncertain_offer = session.scalar(
        select(MarketOffer.id)
        .where(
            or_(
                MarketOffer.status.in_(
                    [
                        "creation_uncertain",
                        "acceptance_uncertain",
                        "accepting",
                    ]
                ),
                (MarketOffer.status == "closed")
                & ~exists().where(
                    MarketTrade.external_offer_id == MarketOffer.external_offer_id,
                    MarketTrade.period_id == MarketOffer.period_id,
                ),
            )
        )
        .limit(1)
    )
    if any(
        value is not None
        for value in (
            provisional,
            missing_custody,
            pending_production,
            missing_trade_units,
            uncertain_offer,
        )
    ):
        raise RecallInvestigationError(
            "Origin batch is not synchronized; custody history is incomplete"
        )
    return {
        "kind": "unaffected",
        "batch": batch,
        "checkedAt": datetime.now(UTC).isoformat(),
        "inventoryComplete": True,
        "retainedCustodyChecked": True,
        "unitsChecked": session.scalar(select(func.count()).select_from(Unit)),
        "tradesChecked": session.scalar(select(func.count()).select_from(MarketTrade)),
    }
