"""Serialize a logical operation across commits and release it on process exit."""

from contextlib import contextmanager
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.orm import Session


@contextmanager
def operation_lock(session: Session, operation: str):
    held = session.info.setdefault("operation_locks", set())
    if operation in held:
        yield True
        return
    key = int.from_bytes(sha256(operation.encode()).digest()[:8], signed=True)
    with session.get_bind().engine.connect() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        connection.commit()
        if not acquired:
            yield False
            return
        held.add(operation)
        try:
            yield True
        finally:
            held.remove(operation)
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
                connection.commit()
            except Exception:
                connection.invalidate()
                raise
