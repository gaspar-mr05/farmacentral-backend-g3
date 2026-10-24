"""Poll active recalls and complete pending recall responses."""

import argparse
import asyncio
import logging
from contextlib import suppress

from app.clients.farma_central import FarmaCentralClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.recalls import RecallWorkerService

logger = logging.getLogger(__name__)


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    async with FarmaCentralClient(settings=settings) as client:
        while True:
            failed = False
            try:
                with SessionLocal() as session:
                    result = await RecallWorkerService(
                        client, session, settings=settings
                    ).poll()
                failed = result.connection_failed or result.failed > 0
                logger.info(
                    "Recall processing: received=%s completed=%s failed=%s",
                    result.received,
                    result.completed,
                    result.failed,
                )
            except Exception:
                failed = True
                logger.error("Recall processing failed", exc_info=True)

            if once:
                if failed:
                    raise SystemExit(1)
                return
            await asyncio.sleep(settings.recall_poll_interval_seconds)


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
