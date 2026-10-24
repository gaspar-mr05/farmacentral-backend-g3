"""Recover a lost messaging response using an explicitly identified official OC ID.

Does not call the LLM or answer the message again. Fetches the existing OC,
validates it against the persisted interpretation and accepts or rejects it.
"""

import argparse
import asyncio
import logging

from app.clients.purchase_orders import PurchaseOrdersClient
from app.db.session import SessionLocal
from app.services.messaging.recovery import MessagingRecoveryService

logger = logging.getLogger(__name__)


async def run(message_id: str, order_id: str) -> None:
    async with PurchaseOrdersClient() as client:
        with SessionLocal() as session:
            order = await MessagingRecoveryService(client, session).recover(
                message_id, order_id
            )
            logger.info(
                "Recovered OC: %s; state: %s", order.external_order_id, order.status
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message_id")
    parser.add_argument(
        "order_id", help="Existing official OC ID, identified independently"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run(args.message_id, args.order_id))
    except Exception:
        logger.error("Recovery failed; no new message or OC was created", exc_info=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
