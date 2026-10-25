"""Bounded PostgreSQL probe, independent of the application's connection pool."""

import logging
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_health_engine():
    return create_engine(
        get_settings().database_url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 3,
            "options": "-cstatement_timeout=2000 -cdefault_transaction_read_only=on",
        },
    )


def database_available() -> bool:
    try:
        with get_health_engine().connect() as connection:
            return connection.scalar(text("SELECT 1")) == 1
    except SQLAlchemyError as exc:
        # Connection errors can contain the DSN. Log only the error class.
        logger.warning("Database readiness failed: error_type=%s", type(exc).__name__)
        return False
