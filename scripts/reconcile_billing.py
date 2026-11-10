"""Continuously observe invoices and bank balance; never emit or pay."""

import argparse
import asyncio
import hashlib
import logging
from contextlib import suppress
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.clients.billing import BillingClient
from app.core.config import get_settings
from app.db.session import engine
from app.models import BusinessPeriod
from app.services.billing import BillingReconciliationService

logger = logging.getLogger(__name__)


async def run(*, period_id: UUID, once: bool = False) -> None:
    settings = get_settings()
    # A session-level advisory lock remains held across commits, but no DB
    # transaction is held open while waiting for SOAP. Unlock before pooling it.
    key = int.from_bytes(
        hashlib.sha256(f"billing:{period_id}".encode()).digest()[:8], signed=True
    )
    with engine.connect() as connection:
        locked = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        connection.commit()
        if not locked:
            raise ValueError("Billing reconciliation already running for this period")
        try:
            with Session(bind=connection, expire_on_commit=False) as session:
                period = session.get(BusinessPeriod, period_id)
                if period is None:
                    raise ValueError(
                        "Billing period not found; create an explicit period first"
                    )
                host = urlsplit(settings.effective_billing_url).hostname or ""
                environment = (
                    "prod"
                    if host.startswith("prod.")
                    else "dev"
                    if host.startswith("dev.")
                    else settings.app_env
                )
                if (
                    period.environment != environment
                    or period.group_id != settings.farma_central_group
                ):
                    raise ValueError(
                        "Billing period environment/group does not match configuration"
                    )
                session.commit()
                async with BillingClient(settings=settings) as client:
                    while True:
                        try:
                            result = await BillingReconciliationService(
                                client, session, period, settings=settings
                            ).poll()
                            logger.info(
                                "Billing reconciliation: queries=%s "
                                "observations=%s failed=%s",
                                result["queries"],
                                result["observations"],
                                result["failed"],
                            )
                        except Exception as exc:
                            session.rollback()
                            logger.error(
                                "Billing reconciliation cycle failed: error=%s",
                                type(exc).__name__,
                            )
                            # A lost DB connection also loses its advisory lock.
                            # Restart under the supervisor to acquire it again.
                            if once or isinstance(exc, SQLAlchemyError):
                                raise SystemExit(1) from None
                        else:
                            if once:
                                if result["failed"]:
                                    raise SystemExit(1)
                                return
                        await asyncio.sleep(settings.billing_poll_interval_seconds)
        finally:
            connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-id", type=UUID)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    configured = args.period_id or get_settings().billing_period_id
    if not configured:
        parser.error(
            "Set BILLING_PERIOD_ID or pass --period-id; "
            "no period is selected automatically"
        )
    try:
        period_id = UUID(str(configured))
    except ValueError:
        parser.error("BILLING_PERIOD_ID must be a UUID")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with suppress(KeyboardInterrupt):
        asyncio.run(run(period_id=period_id, once=args.once))


if __name__ == "__main__":
    main()
