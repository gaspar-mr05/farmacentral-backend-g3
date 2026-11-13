from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BillingOperationStatus,
    BillingTask,
    MarketOffer,
    WorkerHeartbeat,
)


@dataclass(frozen=True)
class QueueSnapshot:
    depth: int
    oldest_at: datetime | None


def billing_queue_snapshot(session: Session, period_id: UUID) -> QueueSnapshot:
    depth, oldest = session.execute(
        select(func.count(), func.min(BillingTask.created_at)).where(
            BillingTask.period_id == period_id,
            BillingTask.status != BillingOperationStatus.CONFIRMED,
        )
    ).one()
    return QueueSnapshot(depth=depth, oldest_at=oldest)


def trades_queue_snapshot(session: Session, period_id: UUID) -> QueueSnapshot:
    depth, oldest = session.execute(
        select(func.count(), func.min(MarketOffer.created_at)).where(
            MarketOffer.period_id == period_id,
            MarketOffer.reconciled_at.is_(None),
            MarketOffer.status != "failed",
        )
    ).one()
    return QueueSnapshot(depth=depth, oldest_at=oldest)


def _row(
    session: Session, period_id: UUID, worker_name: str, now: datetime
) -> WorkerHeartbeat:
    row = session.get(WorkerHeartbeat, (period_id, worker_name))
    if row is None:
        row = WorkerHeartbeat(
            period_id=period_id,
            worker_name=worker_name,
            heartbeat_at=now,
            last_started_at=now,
        )
        session.add(row)
    return row


def mark_started(
    session: Session,
    period_id: UUID,
    worker_name: str,
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    row = _row(session, period_id, worker_name, now)
    row.heartbeat_at = row.last_started_at = now
    session.commit()


def mark_success(
    session: Session,
    period_id: UUID,
    worker_name: str,
    queue: QueueSnapshot,
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    row = _row(session, period_id, worker_name, now)
    row.heartbeat_at = row.last_success_at = now
    row.last_error_type = None
    row.consecutive_failures = 0
    row.queue_depth = queue.depth
    row.oldest_queued_at = queue.oldest_at
    session.commit()


def mark_failure(
    session: Session,
    period_id: UUID,
    worker_name: str,
    error: BaseException | str,
    queue: QueueSnapshot,
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    row = _row(session, period_id, worker_name, now)
    row.heartbeat_at = row.last_failure_at = now
    row.last_error_type = error if isinstance(error, str) else type(error).__name__
    row.consecutive_failures += 1
    row.queue_depth = queue.depth
    row.oldest_queued_at = queue.oldest_at
    session.commit()
