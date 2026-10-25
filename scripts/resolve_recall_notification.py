"""Record official evidence for an ambiguous notification, without calling APIs."""

import argparse
import json
from datetime import datetime

from app.db.session import SessionLocal
from app.services.recalls.notification_recovery import (
    NotificationResolutionError,
    resolve_notification,
)


def aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Use an ISO 8601 timestamp with timezone"
        ) from exc
    if parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("The timestamp requires an explicit timezone")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recall_id")
    parser.add_argument("order_id")
    parser.add_argument("--outcome", choices=("sent", "not-sent"), required=True)
    parser.add_argument(
        "--evidence",
        required=True,
        help="Reference to official proof; never infer the outcome",
    )
    parser.add_argument("--sent-at", type=aware_datetime)
    args = parser.parse_args()
    if not args.evidence.strip():
        parser.error("--evidence cannot be empty")
    if (args.outcome == "sent") != (args.sent_at is not None):
        parser.error("--sent-at is required only with --outcome sent")
    try:
        with SessionLocal() as session:
            result = resolve_notification(
                session,
                args.recall_id,
                args.order_id,
                outcome=args.outcome,
                evidence=args.evidence,
                sent_at=args.sent_at,
            )
    except NotificationResolutionError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
