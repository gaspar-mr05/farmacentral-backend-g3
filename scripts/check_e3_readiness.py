"""Static preflight for an explicitly selected environment; no network or writes."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from defusedxml import ElementTree
from google.oauth2.service_account import Credentials
from sqlalchemy.engine import make_url

from app.core.config import get_settings


def check(settings, *, environment, expected_api_host, wsdl_file=None):
    if environment == "prod" and expected_api_host.startswith("dev."):
        raise ValueError("Production cannot use the development host")
    urls = {
        "farma_central": str(settings.farma_central_base_url),
        "orders": settings.effective_purchase_orders_base_url,
        "trades": settings.effective_trades_base_url,
        "messaging": settings.effective_messaging_base_url,
        "billing": settings.effective_billing_url,
        "wsdl": settings.effective_billing_wsdl_url,
        "checkout": str(settings.checkout_base_url),
    }
    checks = {
        f"endpoint:{name}": urlsplit(url).hostname == expected_api_host
        and urlsplit(url).scheme == "https"
        and urlsplit(url).username is None
        for name, url in urls.items()
    }
    selected = os.environ.get("APP_ENV_FILE")
    checks["single_environment_file"] = bool(selected and Path(selected).is_file())
    checks["declared_environment"] = settings.app_env == environment
    checks["api_secret_configured"] = bool(
        settings.farma_central_api_secret.get_secret_value()
    )
    checks["sftp_password_configured"] = bool(
        settings.effective_sftp_password.get_secret_value()
    )
    checks["sftp_username_configured"] = bool(settings.sftp_username)
    checks["pubsub_identifiers_configured"] = bool(
        settings.pubsub_topic_id and settings.pubsub_subscription_id
    )
    try:
        credentials = Credentials.from_service_account_file(
            str(settings.pubsub_credentials_file)
        )
        checks["pubsub_credentials_valid"] = bool(credentials.service_account_email)
    except (ValueError, OSError, TypeError):
        checks["pubsub_credentials_valid"] = False
    opposite_environment = "dev" if environment == "prod" else "prod"
    opposite = "-dev" if environment == "prod" else "-prod"
    topic = settings.effective_pubsub_topic_id.rsplit("/", 1)[-1]
    checks["pubsub_environment"] = not topic.startswith(
        opposite_environment + "."
    ) and not settings.effective_pubsub_subscription_id.endswith(opposite)
    checks["sftp_environment"] = (
        "desarrollo" not in settings.effective_sftp_username
        if environment == "prod"
        else "produccion" not in settings.effective_sftp_username
    )
    checks["period_selected"] = bool(settings.billing_period_id)
    try:
        addresses = [
            node.get("location")
            for node in ElementTree.parse(wsdl_file).getroot().iter()
            if node.tag.endswith("}address") and node.get("location")
        ]
        checks["wsdl_address_verified"] = bool(addresses) and all(
            value.rstrip("/") == settings.effective_billing_url.rstrip("/")
            for value in addresses
        )
    except Exception:
        checks["wsdl_address_verified"] = False
    checks["spending_limit_explicit"] = (
        settings.replenishment_spending_limit is not None
    )
    checks["invoicing_enabled"] = settings.billing_emit_sales_enabled
    checks["payments_enabled"] = settings.billing_pay_invoices_enabled
    public = str(settings.app_public_url).rstrip("/")
    checks["public_https"] = urlsplit(public).scheme == "https"
    checks["messaging_audience"] = (
        str(settings.effective_messaging_oidc_audience) == public + "/api/messaging"
    )
    checks["recall_audience"] = (
        str(settings.effective_recall_oidc_audience) == public + "/api/recalls"
    )
    database = make_url(settings.database_url)
    return {
        "configuration_valid": all(checks.values()),
        "checks": checks,
        "environment": environment,
        "group": settings.farma_central_group,
        "database": {"host": database.host, "name": database.database},
        "controls": {
            "stock_minimum": settings.replenishment_minimum_stock,
            "stock_target": settings.replenishment_target_stock,
            "spending_limit": str(settings.replenishment_spending_limit),
            "billing_poll_seconds": settings.billing_poll_interval_seconds,
            "billing_requests_per_minute": settings.billing_requests_per_minute,
            "llm_output_tokens": settings.llm_gateway_max_tokens,
        },
        "not_verified": [
            "Official production URLs/credentials and SOAP address after publication",
            "SFTP permissions and Pub/Sub ACLs/topic/subscription for this group",
            "Confirmed reset, separate backed-up database and matching fresh period",
            "Live worker heartbeats, website and cold-start trades/fulfilled orders",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["dev", "prod"])
    parser.add_argument(
        "--expected-api-host",
        required=True,
        help="Copy the official host when published; do not guess",
    )
    parser.add_argument(
        "--wsdl-file",
        type=Path,
        help="Official WSDL saved locally; verifies advertised SOAP address",
    )
    args = parser.parse_args(argv)
    try:
        report = check(
            get_settings(),
            environment=args.environment,
            expected_api_host=args.expected_api_host,
            wsdl_file=args.wsdl_file,
        )
    except Exception as exc:
        print(
            json.dumps({"configuration_valid": False, "error_type": type(exc).__name__})
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["configuration_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
