"""Maintain usable kit stock through official supply and production, without LLM."""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack, suppress
from time import perf_counter

from app.clients.farma_central import (
    FarmaCentralClient,
    FarmaCentralHTTPError,
    FarmaCentralInvalidResponseError,
)
from app.clients.market_prices import MarketPriceClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.trades import TradesClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.production.kits import KitProductionError, KitProductionService
from app.services.production.locking import ProductionBusyError
from app.services.production.orchestration import ProductionError
from app.services.production.replenishment import E3ReplenishmentService
from app.services.trades.periods import market_period

logger = logging.getLogger(__name__)


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(FarmaCentralClient(settings=settings))
        trades = orders = prices = None
        if settings.billing_period_id:
            trades = await stack.enter_async_context(TradesClient(settings=settings))
            orders = await stack.enter_async_context(
                PurchaseOrdersClient(settings=settings)
            )
            prices = await stack.enter_async_context(
                MarketPriceClient(settings=settings)
            )
        while True:
            failed = False
            started = perf_counter()
            retry_delay = settings.replenishment_poll_interval_seconds
            try:
                with SessionLocal() as session:
                    if settings.billing_period_id:
                        period = market_period(session, settings)
                        result = await E3ReplenishmentService(
                            client,
                            trades,
                            orders,
                            prices,
                            session,
                            period,
                            settings=settings,
                        ).poll()
                        failed = result["failed"] > 0
                        logger.info("E3 replenishment: %s", result)
                    else:
                        runs = await KitProductionService(
                            client,
                            session,
                            raw_material_source="supply",
                            require_official_batches=True,
                        ).maintain_kit_stock(
                            minimum=settings.replenishment_minimum_stock,
                            target=settings.replenishment_target_stock,
                            minimum_lifetime_seconds=settings.replenishment_minimum_lifetime_seconds,
                        )
                        logger.info(
                            "Inventory replenishment: kits=%s runs=%s",
                            len(runs),
                            sum(len(value) for value in runs.values()),
                        )
            except FarmaCentralHTTPError as exc:
                logger.error(
                    "Inventory replenishment failed: error=%s http_status=%s",
                    type(exc).__name__,
                    exc.status_code,
                )
                failed = True
                retry_delay = max(
                    retry_delay,
                    60 if exc.status_code == 429 else 0,
                    exc.retry_after_seconds or 0,
                )
            except ProductionBusyError:
                logger.info("Inventory replenishment deferred: packaging is busy")
            except (
                KitProductionError,
                ProductionError,
                FarmaCentralInvalidResponseError,
            ) as exc:
                logger.error(
                    "Inventory replenishment failed (%s): %s", type(exc).__name__, exc
                )
                failed = True
            except Exception as exc:
                # Keep polling after a failed cycle. Do not log external bodies.
                logger.error("Inventory replenishment failed (%s)", type(exc).__name__)
                failed = True
                retry_delay = max(
                    retry_delay, getattr(exc, "retry_after_seconds", None) or 0
                )
            logger.info(
                "Inventory replenishment cycle: failed=%s elapsed_seconds=%.2f",
                failed,
                perf_counter() - started,
            )
            if once:
                if failed:
                    raise SystemExit(1)
                return
            await asyncio.sleep(retry_delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with suppress(KeyboardInterrupt):
        asyncio.run(run(once=args.once))


if __name__ == "__main__":
    main()
