"""Capture business metrics without calling external services."""

import argparse
import json
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import engine
from app.services.billing import BusinessMetricsService
from app.services.billing.periods import billing_period


def capture(*, period_id: UUID, is_closing: bool = False) -> dict:
    settings = get_settings()
    with Session(engine) as session:
        period = billing_period(session, settings, period_id)
        snapshot = BusinessMetricsService(session, period).capture(
            settings.app_commit_sha, is_closing=is_closing
        )
        return snapshot.metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-id", type=UUID)
    parser.add_argument(
        "--closing",
        action="store_true",
        help="Mark this immutable snapshot as the period-closing observation",
    )
    args = parser.parse_args()
    configured = args.period_id or get_settings().billing_period_id
    if not configured:
        parser.error("Set BILLING_PERIOD_ID or pass --period-id")
    try:
        period_id = UUID(str(configured))
    except ValueError:
        parser.error("BILLING_PERIOD_ID must be a UUID")
    print(json.dumps(capture(period_id=period_id, is_closing=args.closing), indent=2))


if __name__ == "__main__":
    main()
