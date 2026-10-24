import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient, FarmaCentralError
from app.core.config import Settings, get_settings
from app.db.inbound_events import record_recall
from app.models import InboundEvent, InboundEventSource, InboundEventStatus
from app.schemas.recalls import RecallWebhookPayload

from .response import RecallResponseService

logger = logging.getLogger(__name__)
PROCESSING_RECOVERY_DELAY = timedelta(minutes=1)


@dataclass
class RecallWorkerResult:
    received: int = 0
    completed: int = 0
    failed: int = 0
    connection_failed: bool = False


class RecallWorkerService:
    def __init__(
        self,
        client: FarmaCentralClient,
        session: Session,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._session = session
        self._settings = settings or get_settings()

    async def poll(self) -> RecallWorkerResult:
        result = RecallWorkerResult()
        await self._receive_active_recalls(result)
        await self._process_pending(result)
        return result

    async def _receive_active_recalls(self, result: RecallWorkerResult) -> None:
        try:
            payload = await self._client.get_active_recalls()
            if not isinstance(payload, list):
                raise ValueError("Farma Central recalls must be a list")
            recalls = [RecallWebhookPayload.model_validate(item) for item in payload]
        except (FarmaCentralError, ValidationError, ValueError) as exc:
            result.connection_failed = True
            logger.warning("Recall polling failed: %s", exc)
            return

        try:
            for recall in recalls:
                if record_recall(self._session, recall):
                    result.received += 1
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    async def _process_pending(self, result: RecallWorkerResult) -> None:
        now = datetime.now(UTC)
        event_ids = self._session.scalars(
            select(InboundEvent.id)
            .where(self._ready(now))
            .order_by(InboundEvent.received_at)
            .limit(self._settings.recall_batch_size)
        ).all()

        for event_id in event_ids:
            event = self._session.scalar(
                select(InboundEvent)
                .where(InboundEvent.id == event_id, self._ready(datetime.now(UTC)))
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if event is None:
                self._session.commit()
                continue
            await self._process_event(event, result)

    async def _process_event(
        self, event: InboundEvent, result: RecallWorkerResult
    ) -> None:
        try:
            recall = RecallWebhookPayload.model_validate(event.payload)
        except ValidationError as exc:
            self._fail(event, "Invalid persisted recall payload")
            self._session.commit()
            result.failed += 1
            logger.warning(
                "Recall event failed permanently: recall_id=%s error=%s",
                event.external_event_id,
                exc,
            )
            return

        now = datetime.now(UTC)
        if recall.deadline <= now:
            self._fail(event, "Recall deadline expired before processing")
            self._session.commit()
            result.failed += 1
            return

        event.status = InboundEventStatus.PROCESSING
        event.attempt_count += 1
        event.last_attempt_at = now
        event.next_retry_at = None
        event.last_error = None
        self._session.commit()

        try:
            await RecallResponseService(self._client, self._session).respond(
                recall.recall_id
            )
        except SQLAlchemyError:
            self._session.rollback()
            raise
        except Exception as exc:
            self._session.rollback()
            event = self._session.get(InboundEvent, event.id)
            self._fail(
                event,
                str(exc),
                retry_at=datetime.now(UTC)
                + timedelta(seconds=self._settings.recall_poll_interval_seconds),
            )
            self._session.commit()
            result.failed += 1
            logger.warning(
                "Recall response failed: recall_id=%s error=%s",
                recall.recall_id,
                exc,
            )
            return

        event = self._session.get(InboundEvent, event.id)
        event.status = InboundEventStatus.PROCESSED
        event.processed_at = datetime.now(UTC)
        event.next_retry_at = None
        event.last_error = None
        self._session.commit()
        result.completed += 1

    @staticmethod
    def _ready(now: datetime):
        return and_(
            InboundEvent.source == InboundEventSource.RECALL,
            or_(
                InboundEvent.status == InboundEventStatus.PENDING,
                and_(
                    InboundEvent.status == InboundEventStatus.FAILED,
                    InboundEvent.next_retry_at <= now,
                ),
                and_(
                    InboundEvent.status == InboundEventStatus.PROCESSING,
                    InboundEvent.last_attempt_at <= now - PROCESSING_RECOVERY_DELAY,
                ),
            ),
        )

    @staticmethod
    def _fail(
        event: InboundEvent, error: str, *, retry_at: datetime | None = None
    ) -> None:
        event.status = InboundEventStatus.FAILED
        event.last_error = error[:1000]
        event.next_retry_at = retry_at
