from urllib.parse import urlsplit

from app.services.billing.periods import billing_period


def market_period(session, settings, period_id=None):
    period = billing_period(session, settings, period_id)
    for url in (
        settings.effective_trades_base_url,
        settings.effective_purchase_orders_base_url,
        str(settings.farma_central_base_url),
    ):
        host = urlsplit(url).hostname or ""
        environment = (
            "prod"
            if host.startswith("prod.")
            else "dev"
            if host.startswith("dev.")
            else settings.app_env
        )
        if environment != period.environment:
            raise ValueError("Market integration environment does not match period")
    return period
