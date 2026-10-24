"""Investigate and respond to a recorded recall."""

import argparse
import asyncio
import logging

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.recalls import RecallResponseService

logger = logging.getLogger(__name__)


async def run(recall_id: str) -> None:
    with SessionLocal() as session:
        async with FarmaCentralClient() as client:
            result = await RecallResponseService(client, session).respond(recall_id)
    logger.info(
        "Recall %s completed: quarantined=%s notified=%s",
        result.recall_id,
        result.quarantined_units,
        result.notified_orders,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recall_id", help="External recall ID already received locally")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        asyncio.run(run(args.recall_id))
    except Exception as exc:
        logger.error("Recall response failed: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
