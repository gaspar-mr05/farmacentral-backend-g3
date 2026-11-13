"""Observe two public sites and local business state; append private JSONL evidence."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.billing.periods import billing_period
from app.services.operational_report import observe


def public_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError(
            "Use a public HTTPS URL without credentials or query"
        )
    return value


def probe(client, url):
    started = perf_counter()
    result = {"url": url, "available": False, "status": None, "error_type": None}
    try:
        with client.stream("GET", url) as response:
            result.update(
                status=response.status_code, available=200 <= response.status_code < 300
            )
    except httpx.HTTPError as exc:
        result["error_type"] = type(exc).__name__
    result["duration_ms"] = round((perf_counter() - started) * 1000, 2)
    return result


def append_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False) + "\n")


def summarize(path, period_id):
    sites = {}
    scopes = {}
    for line in path.open(encoding="utf-8"):
        row = json.loads(line)
        if row["period_id"] != str(period_id):
            continue
        scope = row["measurement_scope"]
        scopes[scope] = scopes.get(scope, 0) + 1
        for name, probe_result in row["sites"].items():
            key = (scope, name, probe_result["url"])
            item = sites.setdefault(
                key,
                {
                    "scope": scope,
                    "site": name,
                    "url": probe_result["url"],
                    "samples": 0,
                    "available": 0,
                    "total_duration_ms": 0,
                    "max_gap_seconds": 0,
                    "last_at": None,
                },
            )
            timestamp = datetime.fromisoformat(row["observed_at"])
            if item["last_at"]:
                item["max_gap_seconds"] = max(
                    item["max_gap_seconds"],
                    (
                        timestamp - datetime.fromisoformat(item["last_at"])
                    ).total_seconds(),
                )
            item["last_at"] = row["observed_at"]
            item["samples"] += 1
            item["available"] += int(probe_result["available"])
            item["total_duration_ms"] += probe_result["duration_ms"]
    for item in sites.values():
        item["observed_availability_ratio"] = item["available"] / item["samples"]
        item["mean_duration_ms"] = item.pop("total_duration_ms") / item["samples"]
    return {
        "period_id": str(period_id),
        "scopes": scopes,
        "sites": list(sites.values()),
        "basis": "observed_samples_only",
    }


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-id", type=UUID)
    parser.add_argument(
        "--customer-url", type=public_url, default=os.getenv("E3_CUSTOMER_SITE_URL")
    )
    parser.add_argument(
        "--traceability-url",
        type=public_url,
        default=os.getenv("E3_TRACEABILITY_SITE_URL"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            os.getenv("E3_MONITOR_OUTPUT", "/var/lib/farmacentral/e3-monitor.jsonl")
        ),
    )
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    period_id = args.period_id or settings.billing_period_id
    if not period_id:
        parser.error("Select BILLING_PERIOD_ID or --period-id")
    if args.summary:
        print(json.dumps(summarize(args.output, period_id), indent=2))
        return 0
    if not args.customer_url or not args.traceability_url:
        parser.error("Configure both public site URLs")
    report = {
        "period_id": str(period_id),
        "environment": settings.app_env,
        "observed_at": datetime.now(UTC).isoformat(),
        "measurement_scope": "unverified",
        "sites": {},
        "database": {"status": "unavailable"},
    }
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        for name, url in (
            ("customer", args.customer_url),
            ("traceability", args.traceability_url),
        ):
            report["sites"][name] = probe(client, url)
    try:
        from app.db.session import engine

        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '15s'"))
            period = billing_period(session, settings, period_id)
            now = datetime.now(UTC)
            report["environment"] = period.environment
            report["period"] = {
                "code": period.code,
                "starts_at": period.starts_at.isoformat(),
                "ends_at": period.ends_at.isoformat() if period.ends_at else None,
            }
            report["measurement_scope"] = (
                "evaluation"
                if period.environment == "prod"
                and period.starts_at <= now
                and (period.ends_at is None or now < period.ends_at)
                else "outside_evaluation"
            )
            report["database"] = {
                "status": "observed",
                **observe(session, period, now=now),
            }
            session.rollback()
    except Exception as exc:
        report["database"]["error_type"] = type(exc).__name__
    append_report(args.output, report)
    alerts = report["database"].get("alerts", [])
    print(
        json.dumps(
            {
                "output": str(args.output),
                "database": report["database"]["status"],
                "alerts": len(alerts),
                "sites_available": all(
                    item["available"] for item in report["sites"].values()
                ),
            }
        )
    )
    return 0 if report["database"]["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
