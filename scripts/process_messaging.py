"""Interpret pending messages, answer them and process any created orders."""

import argparse
import asyncio
import logging
from contextlib import suppress

from sqlalchemy.exc import SQLAlchemyError

from app.clients.llm_gateway import LLMGatewayClient
from app.clients.messaging import MessagingClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.message_interpretation import MessageInterpretationService
from app.services.messaging_processing import MessagingProcessingService

logger = logging.getLogger(__name__)


async def run(*, once: bool = False) -> None:
    settings = get_settings()
    async with (
        MessagingClient(settings=settings) as messaging,
        LLMGatewayClient(settings=settings) as llm,
        PurchaseOrdersClient(settings=settings) as orders,
    ):
        while True:
            delay = settings.messaging_poll_interval_seconds
            try:
                with SessionLocal() as session:
                    interpreter = MessageInterpretationService(llm, session)
                    result = await MessagingProcessingService(
                        messaging,
                        interpreter,
                        orders,
                        session,
                        settings=settings,
                    ).process_pending()
                logger.info(
                    "Messaging processing: processed=%s failed=%s",
                    result.processed,
                    result.failed,
                )
                failed = result.failed > 0
                delay = max(delay, result.retry_after_seconds)
            except SQLAlchemyError:
                logger.error(
                    "Messaging processing failed: database error", exc_info=True
                )
                failed = True

            if once:
                if failed:
                    raise SystemExit(1)
                return
            await asyncio.sleep(delay)


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
