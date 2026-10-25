"""Maintain usable kit stock through official supply and production, without LLM."""

import argparse
import asyncio
import logging
from contextlib import suppress

from app.clients.farma_central import FarmaCentralClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.production.kits import KitProductionError, KitProductionService
from app.services.production.locking import ProductionBusyError

logger = logging.getLogger(__name__)


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    async with FarmaCentralClient(settings=settings) as client:
        while True:
            failed = False
            try:
                with SessionLocal() as session:
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
            except ProductionBusyError:
                logger.info("Inventory replenishment deferred: packaging is busy")
            except KitProductionError as exc:
                logger.error("Inventory replenishment failed: %s", exc)
                failed = True
            except Exception as exc:
                # Keep polling after a failed cycle. Do not log external bodies.
                logger.error("Inventory replenishment failed (%s)", type(exc).__name__)
                failed = True
            if once:
                if failed:
                    raise SystemExit(1)
                return
            await asyncio.sleep(settings.replenishment_poll_interval_seconds)


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
