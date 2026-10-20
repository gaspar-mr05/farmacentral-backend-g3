"""Receive Pub/Sub order notifications and persist their official OC details."""

import argparse
import asyncio
import logging
from contextlib import suppress

from sqlalchemy.exc import SQLAlchemyError

from app.clients.pubsub_orders import PubSubOrdersClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.pubsub_orders import PubSubOrderIngestionService

logger = logging.getLogger(__name__)


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    async with (
        PubSubOrdersClient(settings) as pubsub,
        PurchaseOrdersClient(settings=settings) as orders,
    ):
        await pubsub.ensure_subscription()
        while True:
            try:
                with SessionLocal() as session:
                    result = await PubSubOrderIngestionService(
                        pubsub, orders, session, settings=settings
                    ).poll()
                logger.info(
                    "Pub/Sub poll: received=%s processed=%s failed=%s",
                    result.received,
                    result.processed,
                    result.failed,
                )
                failed = (
                    result.connection_failed
                    or result.acknowledgement_failed
                    or result.failed > 0
                )
                delay = max(
                    settings.pubsub_poll_interval_seconds,
                    result.retry_after_seconds,
                )
            except SQLAlchemyError:
                logger.error("Pub/Sub poll failed: database error", exc_info=True)
                failed = True
                delay = settings.pubsub_poll_interval_seconds
            if once:
                if failed:
                    raise SystemExit(1)
                return
            await asyncio.sleep(delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with suppress(KeyboardInterrupt):
        asyncio.run(run(once=args.once))


if __name__ == "__main__":
    main()
