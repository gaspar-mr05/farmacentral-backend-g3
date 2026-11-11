"""Preview surplus sales. --execute enables the continuous market-sales policy."""

import argparse
import asyncio
import json
import logging
from contextlib import AsyncExitStack, suppress
from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.clients.farma_central import FarmaCentralClient
from app.clients.market_prices import MarketPriceClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.trades import TradesClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.schemas.surplus import SurplusPolicy
from app.services.trades.periods import market_period
from app.services.trades.surplus import SurplusSalesService

logger = logging.getLogger(__name__)


async def run(*, period_id=None, execute=False, once=False, interval=60, policy=None):
    settings = get_settings()
    async with AsyncExitStack() as stack:
        inventory = await stack.enter_async_context(
            FarmaCentralClient(settings=settings)
        )
        trades = await stack.enter_async_context(TradesClient(settings=settings))
        orders = await stack.enter_async_context(
            PurchaseOrdersClient(settings=settings)
        )
        prices = await stack.enter_async_context(MarketPriceClient(settings=settings))
        while True:
            delay = interval
            with SessionLocal() as session:
                period = market_period(session, settings, period_id)
                session.commit()
                try:
                    result = await SurplusSalesService(
                        inventory,
                        trades,
                        orders,
                        prices,
                        session,
                        period,
                        policy=policy,
                    ).cycle(execute=execute)
                    print(
                        json.dumps(result, ensure_ascii=False, indent=2, default=str),
                        flush=True,
                    )
                except Exception as exc:
                    session.rollback()
                    logger.error(
                        "Surplus sales cycle failed: error=%s http_status=%s",
                        type(exc).__name__,
                        getattr(exc, "status_code", None),
                    )
                    if once or not execute or isinstance(exc, SQLAlchemyError):
                        raise SystemExit(1) from None
                    delay = max(delay, getattr(exc, "retry_after_seconds", None) or 0)
                else:
                    if once or not execute:
                        if result["failed"]:
                            raise SystemExit(1)
                        return
            await asyncio.sleep(delay)


def main():
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-id", type=UUID)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--sku", action="append", help="Override the three default direct-access inputs"
    )
    parser.add_argument("--lot-size", type=int, default=20)
    parser.add_argument(
        "--kit-buffer", type=int, default=settings.replenishment_minimum_stock
    )
    parser.add_argument("--raw-buffer-batches", type=int, default=1)
    parser.add_argument("--minimum-margin", type=Decimal, default=Decimal("0.10"))
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.interval_seconds < 30:
        parser.error("Use an interval of at least 30 seconds")
    try:
        options = dict(
            lot_size=args.lot_size,
            kit_buffer=args.kit_buffer,
            raw_buffer_batches=args.raw_buffer_batches,
            minimum_margin=args.minimum_margin,
            minimum_lifetime_seconds=max(
                3660, settings.replenishment_minimum_lifetime_seconds
            ),
        )
        if args.sku:
            options["skus"] = tuple(dict.fromkeys(args.sku))
        policy = SurplusPolicy(**options)
    except ValidationError as exc:
        parser.error(str(exc))
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with suppress(KeyboardInterrupt):
        asyncio.run(
            run(
                period_id=args.period_id,
                execute=args.execute,
                once=args.once,
                interval=args.interval_seconds,
                policy=policy,
            )
        )


if __name__ == "__main__":
    main()
