import logging

from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.models import BillingTask, BillingTaskKind

from .periods import billing_period

logger = logging.getLogger(__name__)


def enqueue_sale(session, period, external_order_id):
    session.execute(
        insert(BillingTask)
        .values(
            period_id=period.id,
            kind=BillingTaskKind.EMIT_INVOICE,
            external_order_id=external_order_id,
        )
        .on_conflict_do_nothing(constraint="uq_billing_task_order")
    )


def enqueue_payment(session, invoice):
    session.execute(
        insert(BillingTask)
        .values(
            period_id=invoice.period_id,
            kind=BillingTaskKind.PAY_INVOICE,
            invoice_id=invoice.id,
        )
        .on_conflict_do_nothing(constraint="uq_billing_task_invoice")
    )


def enqueue_fulfilled_sale(session, order, *, settings=None):
    """Join the order transaction; remote billing happens in the worker."""
    settings = settings or get_settings()
    if not settings.billing_period_id or not order.external_order_id:
        return
    try:
        period = billing_period(session, settings)
    except ValueError:
        logger.warning("Cannot enqueue billing: configure a matching accounting period")
        return
    if (
        order.created_at
        and order.created_at >= period.starts_at
        and (period.ends_at is None or order.created_at < period.ends_at)
    ):
        enqueue_sale(session, period, order.external_order_id)
