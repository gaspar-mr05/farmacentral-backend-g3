import base64
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.clients.pubsub_orders import PubSubOrdersClient, PubSubOrdersError
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.purchase_orders_exceptions import (
    PurchaseOrdersAuthenticationError,
    PurchaseOrdersError,
    PurchaseOrdersHTTPError,
)
from app.core.config import Settings, get_settings
from app.db.external_orders import InvalidExternalOrderError, import_external_order
from app.db.inbound_events import record_pubsub_messages
from app.models import InboundEvent, InboundEventSource, InboundEventStatus, OrderSource
from app.schemas.pubsub_orders import InvalidPubSubOrderError, extract_order_id

logger = logging.getLogger(__name__)


@dataclass
class PubSubPollResult:
    received: int = 0
    processed: int = 0
    failed: int = 0
    connection_failed: bool = False
    acknowledgement_failed: bool = False
    retry_after_seconds: float = 0


class PubSubOrderIngestionService:
    def __init__(
        self,
        pubsub: PubSubOrdersClient,
        orders: PurchaseOrdersClient,
        session: Session,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._pubsub = pubsub
        self._orders = orders
        self._session = session
        self._settings = settings or get_settings()

    async def poll(self) -> PubSubPollResult:
        result = PubSubPollResult()
        try:
            try:
                messages = await self._pubsub.pull()
            except PubSubOrdersError as exc:
                messages = []
                result.connection_failed = True
                logger.warning("Pub/Sub pull failed: %s", exc)
            else:
                result.received = record_pubsub_messages(self._session, messages)
                self._session.commit()
                try:
                    await self._pubsub.acknowledge(
                        [message.ack_id for message in messages]
                    )
                except PubSubOrdersError as exc:
                    result.acknowledgement_failed = True
                    logger.warning("Pub/Sub acknowledgement failed: %s", exc)

            await self._process_pending(result)
            return result
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _ready():
        return and_(
            InboundEvent.source == InboundEventSource.PUBSUB,
            or_(
                InboundEvent.status == InboundEventStatus.PENDING,
                and_(
                    InboundEvent.status == InboundEventStatus.FAILED,
                    InboundEvent.next_retry_at <= datetime.now(UTC),
                ),
            ),
        )

    async def _process_pending(self, result: PubSubPollResult) -> None:
        event_ids = self._session.scalars(
            select(InboundEvent.id)
            .where(self._ready())
            .order_by(InboundEvent.received_at)
            .limit(self._settings.pubsub_batch_size)
        ).all()
        for event_id in event_ids:
            event = self._session.scalar(
                select(InboundEvent)
                .where(InboundEvent.id == event_id, self._ready())
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if event is None:
                self._session.commit()
                continue

            event.status = InboundEventStatus.PROCESSING
            event.attempt_count += 1
            event.last_attempt_at = datetime.now(UTC)
            pause = False
            try:
                try:
                    data = base64.b64decode(event.payload["data_base64"], validate=True)
                except (KeyError, ValueError, TypeError) as exc:
                    raise InvalidPubSubOrderError(
                        "Invalid persisted Pub/Sub payload"
                    ) from exc
                order_id = extract_order_id(data)
                event.payload = {**event.payload, "order_id": order_id}
                detail = await self._orders.get_order(order_id)
                order = import_external_order(
                    self._session,
                    detail,
                    source=OrderSource.PUBSUB,
                    supplier_id=str(self._settings.farma_central_group),
                )
            except (InvalidPubSubOrderError, InvalidExternalOrderError) as exc:
                self._fail(event, str(exc))
                result.failed += 1
            except PurchaseOrdersError as exc:
                retry_delay = self._retry_delay(exc)
                self._fail(event, str(exc), retry_after=retry_delay)
                result.failed += 1
                pause = isinstance(exc, PurchaseOrdersAuthenticationError) or (
                    isinstance(exc, PurchaseOrdersHTTPError) and exc.status_code == 429
                )
                if pause:
                    result.retry_after_seconds = retry_delay or 0
            else:
                event.order = order
                event.status = InboundEventStatus.PROCESSED
                event.processed_at = datetime.now(UTC)
                event.next_retry_at = None
                event.last_error = None
                result.processed += 1
                logger.info(
                    "Pub/Sub order imported: message_id=%s order_id=%s",
                    event.external_event_id,
                    order_id,
                )
            self._session.commit()
            if pause:
                break

    def _retry_delay(self, error: PurchaseOrdersError) -> float | None:
        delay = self._settings.pubsub_poll_interval_seconds
        if isinstance(error, PurchaseOrdersHTTPError):
            if (
                not isinstance(error, PurchaseOrdersAuthenticationError)
                and 400 <= error.status_code < 500
                and error.status_code != 429
            ):
                return None
            retry_after = error.retry_after_seconds
            if retry_after is not None and math.isfinite(retry_after):
                delay = max(delay, retry_after)
        return delay

    @staticmethod
    def _fail(
        event: InboundEvent, error: str, *, retry_after: float | None = None
    ) -> None:
        event.status = InboundEventStatus.FAILED
        event.last_error = error
        event.next_retry_at = (
            datetime.now(UTC) + timedelta(seconds=retry_after)
            if retry_after is not None
            else None
        )
        logger.warning(
            "Pub/Sub event failed: message_id=%s order_id=%s error=%s",
            event.external_event_id,
            event.payload.get("order_id"),
            error,
        )
