"""Read-only remote reconciliation of known market offers."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.models import CustodyEvent, MarketOffer, MarketRole, MarketTrade
from app.services.inventory.sync import InventorySyncService

from .custody import TradeCustodyService

logger = logging.getLogger(__name__)


class TradeReconciliationService:
    def __init__(
        self, trades, orders, inventory, session, period, *, interval=60, batch_size=50
    ):
        self.trades = trades
        self.inventory = inventory
        self.session = session
        self.period = period
        self.interval = interval
        self.batch_size = batch_size
        self.custody = TradeCustodyService(
            session,
            farma_central=inventory,
            trades=trades,
            purchase_orders=orders,
            period=period,
        )

    async def poll(self, *, now=None):
        now = now or datetime.now(UTC)
        result = {"checked": 0, "closed": 0, "released": 0, "pending": 0, "failed": 0}
        cooldown = self.session.scalar(
            select(func.max(MarketOffer.next_retry_at)).where(
                MarketOffer.period_id == self.period.id,
                MarketOffer.http_status.in_([401, 403, 429]),
            )
        )
        if cooldown and cooldown > now:
            self.session.commit()
            return result | {"blocked": 1, "custody_pending": self._custody_pending()}
        due = self.session.scalars(
            select(MarketOffer)
            .where(
                MarketOffer.period_id == self.period.id,
                MarketOffer.reconciled_at.is_(None),
                MarketOffer.status != "failed",
                or_(
                    MarketOffer.next_retry_at.is_(None),
                    MarketOffer.next_retry_at <= now,
                ),
            )
            .order_by(MarketOffer.last_checked_at.asc().nullsfirst(), MarketOffer.id)
            .limit(self.batch_size)
        ).all()
        ids = [offer.id for offer in due]
        self.session.commit()
        for offer_id in ids:
            offer = self.session.get(MarketOffer, offer_id)
            result["checked"] += 1
            offer.attempt_count += 1
            offer.last_checked_at = now
            self.session.commit()
            try:
                if not offer.external_offer_id:
                    # No idempotency key or complete per-group listing is documented.
                    # A similar SKU/price/time is not proof of this POST's result.
                    raise ValueError(
                        "Official offer ID unavailable; requires verified association"
                    )
                self.session.commit()
                official = await self.trades.get_offer(offer.external_offer_id)
                trade = await self.custody.reconcile_offer(official)
                if trade and trade.role == MarketRole.BUYER:
                    # Recognize receipt/overflow without waiting for an invoice.
                    await InventorySyncService(
                        self.inventory, self.session
                    ).synchronize()
                if official.status in {"cancelled", "expired", "closed"}:
                    offer.reconciled_at = now
                    result["closed" if trade else "released"] += 1
                else:
                    result["pending"] += 1
                offer.attempt_count = 0
                offer.last_error = offer.http_status = None
                offer.next_retry_at = (
                    None
                    if offer.reconciled_at
                    else now + timedelta(seconds=self.interval)
                )
                self.session.commit()
            except SQLAlchemyError:
                self.session.rollback()
                raise  # A lost DB connection also loses the worker advisory lock.
            except Exception as exc:
                self.session.rollback()
                offer = self.session.get(MarketOffer, offer_id)
                status = getattr(exc, "status_code", None)
                delay = max(
                    self.interval * 2 ** min(max(offer.attempt_count - 1, 0), 4),
                    getattr(exc, "retry_after_seconds", None) or 0,
                    300 if status in {401, 403} else 0,
                )
                offer.last_error = (
                    "Official offer ID unavailable; requires verified association"
                    if not offer.external_offer_id
                    else type(exc).__name__
                )
                offer.http_status = status
                offer.next_retry_at = now + timedelta(seconds=delay)
                self.session.commit()
                result["failed"] += 1
                logger.warning(
                    "Market reconciliation pending: local_offer_id=%s offer_id=%s "
                    "error=%s http_status=%s",
                    offer.id,
                    offer.external_offer_id,
                    type(exc).__name__,
                    status,
                )
                if status in {401, 403, 429}:
                    break
        return result | {"blocked": 0, "custody_pending": self._custody_pending()}

    def _custody_pending(self):
        evidence_count = (
            select(func.count(CustodyEvent.id))
            .where(CustodyEvent.market_trade_id == MarketTrade.id)
            .correlate(MarketTrade)
            .scalar_subquery()
        )
        count = self.session.scalar(
            select(func.count())
            .select_from(MarketTrade)
            .where(
                MarketTrade.period_id == self.period.id,
                evidence_count != MarketTrade.quantity,
            )
        )
        self.session.commit()
        return count
