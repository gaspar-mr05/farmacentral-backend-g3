from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

# One packaging area is shared by all recipes and manual/automatic workers.
PRODUCTION_LOCK = 3410392026
SESSION_KEY = "farmacentral_production_lock"


class ProductionBusyError(Exception):
    pass


@contextmanager
def production_lock(session: Session) -> Iterator[None]:
    if session.info.get(SESSION_KEY):
        yield
        return
    # A separate connection keeps this session-level lock through service commits.
    with session.get_bind().engine.connect() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": PRODUCTION_LOCK}
        )
        connection.commit()
        if not acquired:
            raise ProductionBusyError("Another process is using the packaging area")
        session.info[SESSION_KEY] = True
        try:
            yield
        finally:
            session.info.pop(SESSION_KEY, None)
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": PRODUCTION_LOCK}
                )
                connection.commit()
            except Exception:
                connection.invalidate()
                raise
