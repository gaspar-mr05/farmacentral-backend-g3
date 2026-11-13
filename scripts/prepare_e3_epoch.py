"""Check or initialize a new epoch in an explicitly selected empty database."""

import argparse
import json
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import create_engine, exists, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import Base, BusinessPeriod
from scripts.validate_e2_flow import database_check


def environment_of(settings):
    host = urlsplit(str(settings.farma_central_base_url)).hostname
    if host == "dev.proyecto.2026-2.tallerdeintegracion.cl":
        return "dev"
    if host in {
        "prod.proyecto.2026-2.tallerdeintegracion.cl",
        "proyecto.2026-2.tallerdeintegracion.cl",
    }:
        return "prod"
    raise ValueError("Use an official, explicitly configured environment")


def prepare(
    session,
    settings,
    *,
    environment,
    code,
    starts_at,
    ends_at,
    create=False,
    reset_reference=None,
):
    if settings.app_env != environment or environment_of(settings) != environment:
        raise ValueError("Configured environment does not match requested epoch")
    if (
        starts_at.utcoffset() is None
        or ends_at.utcoffset() is None
        or ends_at <= starts_at
    ):
        raise ValueError("Use timezone-aware epoch dates with end after start")
    if not code.strip():
        raise ValueError("Epoch code is required")
    # Serialize check+creation. This does not stop external workers; operators
    # must keep services stopped while initializing a genuinely new DB.
    session.execute(text("SELECT pg_advisory_xact_lock(671025)"))
    if database_check(session)["status"] != "passed":
        raise ValueError("Apply migrations to the new database first")
    occupied = [
        table.name
        for table in Base.metadata.sorted_tables
        if session.scalar(select(exists().select_from(table)))
    ]
    if occupied:
        raise ValueError(
            "Epoch requires an empty application database; "
            "preserve history and select a new DB"
        )
    if create and not (reset_reference and reset_reference.strip()):
        raise ValueError(
            "Provide an explicit confirmed reset reference before creation"
        )
    result = {
        "state": "empty_database_verified",
        "environment": environment,
        "group": settings.farma_central_group,
        "created": False,
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
    }
    if create:
        period = BusinessPeriod(
            code=code,
            environment=environment,
            group_id=settings.farma_central_group,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        session.add(period)
        session.flush()
        result.update(
            state="epoch_created",
            created=True,
            period_id=str(period.id),
            reset_reference=reset_reference,
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["dev", "prod"])
    parser.add_argument("--code", required=True)
    parser.add_argument("--starts-at", required=True, type=datetime.fromisoformat)
    parser.add_argument("--ends-at", required=True, type=datetime.fromisoformat)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--reset-reference")
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = get_settings()
        engine = create_engine(
            settings.database_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 3},
        )
        with Session(engine) as session:
            report = prepare(
                session,
                settings,
                environment=args.environment,
                code=args.code,
                starts_at=args.starts_at,
                ends_at=args.ends_at,
                create=args.create,
                reset_reference=args.reset_reference,
            )
            if args.create:
                session.commit()
            else:
                session.rollback()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Do not expose credentials or SQL parameters from DB/configuration errors.
        print(
            json.dumps(
                {
                    "state": "not_prepared",
                    "error_type": type(exc).__name__,
                }
            )
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
