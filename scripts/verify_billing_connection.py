"""Read-only SOAP probe. Cannot emit invoices or pay them."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from app.clients.billing import BillingClient, BillingError
from app.core.config import get_settings
from app.models.billing import InvoiceSide, InvoiceStatus


async def run(*, include_invoices: bool = False) -> dict:
    settings = get_settings()
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "billing_read_only",
        "host": urlsplit(settings.effective_billing_url).hostname,
        "llm_calls": 0,
        "business_mutations": 0,
        "checks": {},
    }
    try:
        async with BillingClient(settings=settings) as client:
            statement = await client.get_bank_statement()
            report["checks"]["bank_statement"] = {
                "status": "passed",
                "group": statement.group,
                "balance": str(statement.balance),
            }
            if include_invoices:
                end = datetime.now(UTC)
                for side in InvoiceSide:
                    for status in InvoiceStatus:
                        invoices = await client.get_invoices(
                            side=side,
                            status=status,
                            from_date=end - timedelta(days=1),
                            to_date=end,
                        )
                        report["checks"][f"invoices:{side.value}:{status.value}"] = {
                            "status": "passed",
                            "count": len(invoices),
                        }
    except BillingError as exc:
        report["checks"]["billing_error"] = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "operation": exc.operation,
            "http_status": exc.status_code,
            "outcome_uncertain": exc.outcome_uncertain,
            "retry_after_seconds": exc.retry_after_seconds,
        }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-invoices", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = asyncio.run(run(include_invoices=args.include_invoices))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return int(any(item["status"] == "failed" for item in report["checks"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
