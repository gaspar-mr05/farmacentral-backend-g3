"""Investigate a recorded recall using local inventory and production lineage."""

import argparse
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.db.recalls import RecallSnapshotConflictError
from app.db.session import SessionLocal
from app.services.recall_investigation import (
    RecallInvestigationError,
    RecallInvestigationService,
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recall_id", help="External recall ID already received locally")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        with SessionLocal() as session:
            result = RecallInvestigationService(session).investigate(args.recall_id)
    except (RecallInvestigationError, RecallSnapshotConflictError) as exc:
        logger.error("Recall investigation failed: %s", exc)
        raise SystemExit(1) from exc
    except SQLAlchemyError as exc:
        logger.error("Recall investigation failed: database error")
        raise SystemExit(1) from exc
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
