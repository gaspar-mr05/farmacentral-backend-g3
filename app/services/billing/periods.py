from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select

from app.models import BillingSyncState, BusinessPeriod


def billing_period(session, settings, period_id=None) -> BusinessPeriod:
    selected = period_id or settings.billing_period_id
    if not selected:
        raise ValueError("Select a billing period explicitly")
    period = session.get(BusinessPeriod, UUID(str(selected)))
    if period is None:
        raise ValueError("Billing period not found; create an explicit period first")
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
    return period


def billing_cooldown(session, period_id):
    return session.scalar(
        select(func.max(BillingSyncState.next_retry_at)).where(
            BillingSyncState.period_id == period_id,
            (BillingSyncState.http_status.in_([401, 403, 429]))
            | (BillingSyncState.last_error == "BillingRateLimitError"),
        )
    )
