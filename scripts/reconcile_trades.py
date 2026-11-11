"""Follow known market offers using GETs; enqueue sales and synchronize receipts."""

import argparse
import asyncio
import hashlib
import logging
from contextlib import AsyncExitStack, suppress
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.trades import TradesClient
from app.core.config import get_settings
from app.db.session import engine
from app.services.trades.periods import market_period
from app.services.trades.reconciliation import TradeReconciliationService

logger = logging.getLogger(__name__)


async def run(*, period_id: UUID, once=False, interval=60, batch_size=50):
    settings = get_settings()
    key = int.from_bytes(
        hashlib.sha256(f"trades:{period_id}".encode()).digest()[:8], signed=True
    )
    with engine.connect() as connection:
        locked = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        connection.commit()
        if not locked:
            raise ValueError("Market reconciliation already running for this period")
        try:
            with Session(bind=connection, expire_on_commit=False) as session:
                period = market_period(session, settings, period_id)
                session.commit()
                async with AsyncExitStack() as stack:
                    trades = await stack.enter_async_context(
                        TradesClient(settings=settings)
                    )
                    orders = await stack.enter_async_context(
                        PurchaseOrdersClient(settings=settings)
                    )
                    inventory = await stack.enter_async_context(
                        FarmaCentralClient(settings=settings)
                    )
                    service = TradeReconciliationService(
                        trades,
                        orders,
                        inventory,
                        session,
                        period,
                        interval=interval,
                        batch_size=batch_size,
                    )
                    while True:
                        try:
                            result = await service.poll()
                            logger.info(
                                "Market reconciliation: checked=%s closed=%s "
                                "released=%s "
                                "pending=%s failed=%s blocked=%s custody_pending=%s",
                                result["checked"],
                                result["closed"],
                                result["released"],
                                result["pending"],
                                result["failed"],
                                result["blocked"],
                                result["custody_pending"],
                            )
                        except Exception as exc:
                            session.rollback()
                            logger.error(
                                "Market reconciliation cycle failed: error=%s",
                                type(exc).__name__,
                            )
                            if once or isinstance(exc, SQLAlchemyError):
                                raise SystemExit(1) from None
                        else:
                            if once:
                                if result["failed"] or result["blocked"]:
                                    raise SystemExit(1)
                                return
                        await asyncio.sleep(interval)
        finally:
            connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-id", type=UUID)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    if args.interval_seconds < 30 or not 1 <= args.batch_size <= 200:
        parser.error("Use interval >= 30 seconds and batch size between 1 and 200")
    selected = args.period_id or get_settings().billing_period_id
    if not selected:
        parser.error("Set BILLING_PERIOD_ID or pass --period-id")
    try:
        period_id = UUID(str(selected))
    except ValueError:
        parser.error("BILLING_PERIOD_ID must be a UUID")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with suppress(KeyboardInterrupt):
        asyncio.run(
            run(
                period_id=period_id,
                once=args.once,
                interval=args.interval_seconds,
                batch_size=args.batch_size,
            )
        )


if __name__ == "__main__":
    main()
