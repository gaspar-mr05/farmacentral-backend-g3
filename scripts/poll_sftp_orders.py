"""Receive sFTP order notifications and persist their official OC details."""

import argparse
import asyncio
import logging
from contextlib import suppress

from sqlalchemy.exc import SQLAlchemyError

from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.sftp_orders import SFTPOrdersClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.sftp_orders import SFTPOrderIngestionService

logger = logging.getLogger(__name__)


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    sftp = SFTPOrdersClient(settings)
    async with PurchaseOrdersClient(settings=settings) as orders:
        while True:
            delay = settings.sftp_poll_interval_seconds
            try:
                with SessionLocal() as session:
                    result = await SFTPOrderIngestionService(
                        sftp, orders, session, settings=settings
                    ).poll()
                logger.info(
                    "sFTP poll: received=%s processed=%s failed=%s",
                    result.received,
                    result.processed,
                    result.failed,
                )
                failed = result.connection_failed or result.failed > 0
                delay = max(delay, result.retry_after_seconds)
            except SQLAlchemyError:
                logger.error("sFTP poll failed: database error", exc_info=True)
                failed = True
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
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with suppress(KeyboardInterrupt):
        asyncio.run(run(once=args.once))


if __name__ == "__main__":
    main()
