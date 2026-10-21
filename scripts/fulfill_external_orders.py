"""Fulfill accepted external purchase orders before their deadlines."""

import argparse
import asyncio
import logging
from contextlib import suppress

from sqlalchemy.exc import SQLAlchemyError

from app.clients.farma_central import FarmaCentralClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.external_order_fulfillment import ExternalOrderFulfillmentService

logger = logging.getLogger(__name__)


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    async with (
        FarmaCentralClient(settings=settings) as farma_central,
        PurchaseOrdersClient(settings=settings) as purchase_orders,
    ):
        while True:
            failed = False
            try:
                with SessionLocal() as session:
                    result = await ExternalOrderFulfillmentService(
                        farma_central,
                        purchase_orders,
                        session,
                        settings=settings,
                    ).process_pending()
                logger.info(
                    "External orders: completed=%s pending=%s failed=%s",
                    result.completed,
                    result.pending,
                    result.failed,
                )
                failed = result.failed > 0
            except SQLAlchemyError:
                logger.error(
                    "External order fulfillment failed: database error",
                    exc_info=True,
                )
                failed = True

            if once:
                if failed:
                    raise SystemExit(1)
                return
            await asyncio.sleep(settings.external_order_poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process once and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with suppress(KeyboardInterrupt):
        asyncio.run(run(once=args.once))


if __name__ == "__main__":
    main()
