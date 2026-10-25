"""Resolve an ambiguous notification only from operator-provided evidence."""

from datetime import UTC, datetime

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session

from app.db.operation_locks import operation_lock
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    Order,
    Recall,
    RecallNotification,
    RecallNotificationStatus,
    RecallOrder,
)


class NotificationResolutionError(ValueError):
    pass


def resolve_notification(
    session: Session,
    recall_id: str,
    order_id: str,
    *,
    outcome: str,
    evidence: str,
    sent_at: datetime | None = None,
) -> dict:
    now = datetime.now(UTC)
    if outcome not in {"sent", "not-sent"} or not evidence.strip():
        raise NotificationResolutionError(
            "An outcome and official evidence are required"
        )
    if (outcome == "sent") != (sent_at is not None):
        raise NotificationResolutionError("Only a sent notification requires sent_at")
    if sent_at is not None and (sent_at.utcoffset() is None or sent_at > now):
        raise NotificationResolutionError(
            "sent_at must have a timezone and cannot be in the future"
        )
    with operation_lock(session, f"recall:{recall_id}") as acquired:
        if not acquired:
            raise NotificationResolutionError("Recall is already being processed")
        try:
            event = session.scalar(
                select(InboundEvent)
                .where(
                    InboundEvent.source == InboundEventSource.RECALL,
                    InboundEvent.external_event_id == recall_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            recall = session.scalar(
                select(Recall)
                .where(Recall.recall_id == recall_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if recall is None:
                raise NotificationResolutionError("Recall was not found")
            notification = session.scalar(
                select(RecallNotification)
                .join(RecallOrder)
                .join(Order)
                .where(
                    RecallOrder.recall_id == recall.id,
                    or_(
                        Order.external_order_id == order_id,
                        cast(Order.id, String) == order_id,
                    ),
                )
                .with_for_update(of=RecallNotification)
                .execution_options(populate_existing=True)
            )
            if notification is None or notification.attempt_count == 0:
                raise NotificationResolutionError(
                    "No notification attempt exists for this order and recall"
                )
            if notification.status == RecallNotificationStatus.SENT:
                if outcome != "sent" or notification.sent_at != sent_at:
                    raise NotificationResolutionError(
                        "Cannot overwrite a confirmed notification"
                    )
                session.commit()
                return {
                    "recall_id": recall_id,
                    "order_id": order_id,
                    "outcome": outcome,
                    "requeued": False,
                }
            response = notification.response or {}
            if notification.status != RecallNotificationStatus.PENDING and response.get(
                "delivery_state"
            ) not in {None, "uncertain"}:
                raise NotificationResolutionError(
                    "This notification is not awaiting reconciliation"
                )
            if (
                sent_at is not None
                and not recall.issued_at <= sent_at <= recall.deadline
            ):
                raise NotificationResolutionError(
                    "The confirmed notification must be within the recall window"
                )
            notification.status = (
                RecallNotificationStatus.SENT
                if outcome == "sent"
                else RecallNotificationStatus.FAILED
            )
            notification.sent_at = sent_at
            notification.last_error = None
            notification.response = {
                "delivery_state": "confirmed" if outcome == "sent" else "rejected",
                "resolutions": [
                    *response.get("resolutions", []),
                    {
                        "outcome": outcome,
                        "evidence": evidence.strip(),
                        "recorded_at": now.isoformat(),
                        "sent_at": sent_at.isoformat() if sent_at else None,
                    },
                ],
            }
            requeued = event is not None and recall.deadline > now
            if requeued:
                event.status = InboundEventStatus.PENDING
                event.next_retry_at = None
                event.last_error = None
            session.commit()
            return {
                "recall_id": recall_id,
                "order_id": order_id,
                "outcome": outcome,
                "requeued": requeued,
            }
        except Exception:
            session.rollback()
            raise
