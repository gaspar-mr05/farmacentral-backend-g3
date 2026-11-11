"""Reconcile billing with opt-in sales invoicing and invoice payments."""

import argparse
import asyncio
import hashlib
import logging
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.clients.billing import BillingClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.core.config import get_settings
from app.core.version import resolve_commit_sha
from app.db.session import engine
from app.services.billing import BillingReconciliationService, BusinessMetricsService
from app.services.billing.payments import InvoicePaymentService
from app.services.billing.periods import billing_period
from app.services.billing.sales import SalesInvoicingService

logger = logging.getLogger(__name__)


async def run(
    *,
    period_id: UUID,
    once: bool = False,
    emit_sales: bool = False,
    pay_invoices: bool = False,
    full_sync: bool = False,
) -> None:
    settings = get_settings()
    commit_sha = resolve_commit_sha(settings.app_commit_sha)
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
                period = billing_period(session, settings, period_id)
                session.commit()
                async with AsyncExitStack() as stack:
                    client = await stack.enter_async_context(
                        BillingClient(settings=settings)
                    )
                    orders = (
                        await stack.enter_async_context(
                            PurchaseOrdersClient(settings=settings)
                        )
                        if emit_sales
                        else None
                    )
                    while True:
                        try:
                            reconciliation = BillingReconciliationService(
                                client, session, period, settings=settings
                            )
                            result = (
                                await reconciliation.poll(full=True)
                                if full_sync
                                else await reconciliation.poll()
                            )
                            logger.info(
                                "Billing reconciliation: queries=%s "
                                "observations=%s failed=%s",
                                result["queries"],
                                result["observations"],
                                result["failed"],
                            )
                            reads_failed = result["failed"]
                            if pay_invoices:
                                payments = await InvoicePaymentService(
                                    client, session, period, settings=settings
                                ).poll()
                                logger.info(
                                    "Invoice payments: confirmed=%s pending=%s "
                                    "uncertain=%s overdue=%s",
                                    payments["confirmed"],
                                    payments["pending"],
                                    payments["uncertain"],
                                    payments["overdue"],
                                )
                                result["failed"] += bool(
                                    payments["pending"] or payments["uncertain"]
                                )
                            if emit_sales and not reads_failed:
                                sales = await SalesInvoicingService(
                                    client, orders, session, period, settings=settings
                                ).poll()
                                logger.info(
                                    "Sales invoicing: confirmed=%s "
                                    "missing=%s uncertain=%s",
                                    sales["confirmed"],
                                    sales["missing"],
                                    sales["uncertain"],
                                )
                                result["failed"] += bool(
                                    sales["missing"] or sales["uncertain"]
                                )
                            snapshot = BusinessMetricsService(session, period).capture(
                                commit_sha,
                                minimum_interval=timedelta(
                                    seconds=settings.business_metrics_snapshot_interval_seconds
                                ),
                            )
                            if snapshot:
                                metrics = snapshot.metrics
                                logger.info(
                                    "Business metrics: margin=%s income=%s "
                                    "costs=%s pending_payments=%s",
                                    metrics["margin"],
                                    metrics["invoiced_income"],
                                    metrics["invoiced_costs"],
                                    metrics["pending_payments"]["count"],
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
    parser.add_argument(
        "--full",
        action="store_true",
        help="Request a complete invoice scan; resumes unfinished windows",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Override emission/payment settings and only reconcile observations",
    )
    parser.add_argument(
        "--emit-sales",
        action="store_true",
        help="Issue invoices for officially fulfilled sales (remote mutation)",
    )
    parser.add_argument(
        "--pay-invoices",
        action="store_true",
        help="Pay official client invoices (remote mutation)",
    )
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
        asyncio.run(
            run(
                period_id=period_id,
                once=args.once,
                emit_sales=not args.read_only
                and (args.emit_sales or get_settings().billing_emit_sales_enabled),
                pay_invoices=not args.read_only
                and (args.pay_invoices or get_settings().billing_pay_invoices_enabled),
                full_sync=args.full,
            )
        )


if __name__ == "__main__":
    main()
