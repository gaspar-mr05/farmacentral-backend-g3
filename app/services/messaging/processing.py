import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.clients.farma_central import (
    FarmaCentralAuthenticationError,
    FarmaCentralError,
    FarmaCentralHTTPError,
)
from app.clients.messaging import (
    MessagingAlreadyRespondedError,
    MessagingAuthenticationError,
    MessagingClient,
    MessagingError,
    MessagingHTTPError,
)
from app.clients.purchase_orders import (
    PurchaseOrdersAuthenticationError,
    PurchaseOrdersClient,
    PurchaseOrdersError,
    PurchaseOrdersHTTPError,
)
from app.core.config import Settings, get_settings
from app.db.external_orders import InvalidExternalOrderError
from app.db.operation_locks import operation_lock
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
    OrderSource,
)
from app.schemas.messaging import MessagingWebhookPayload
from app.services.orders import (
    InvalidExternalOrderStateError,
    OrderIngestionService,
    OrderProcessingService,
)

from .interpretation import MessageInterpretationService

logger = logging.getLogger(__name__)
PROCESSING_RECOVERY_DELAY = timedelta(minutes=1)
MESSAGE_RESPONSE_WINDOW = timedelta(minutes=30)


@dataclass
class MessagingProcessingResult:
    processed: int = 0
    failed: int = 0
    retry_after_seconds: float = 0


class MessagingProcessingService:
    def __init__(
        self,
        messaging: MessagingClient,
        interpreter: MessageInterpretationService,
        orders: PurchaseOrdersClient,
        session: Session,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._messaging = messaging
        self._interpreter = interpreter
        self._session = session
        self._settings = settings or get_settings()
        self._ingestion = OrderIngestionService(
            orders,
            session,
            supplier_id=str(self._settings.farma_central_group),
        )
        self._order_processing = OrderProcessingService(orders, session)

    async def process_pending(self) -> MessagingProcessingResult:
        result = MessagingProcessingResult()
        now = datetime.now(UTC)
        self._expire_old_messages(result, now)
        events = self._session.execute(
            select(InboundEvent.id, InboundEvent.external_event_id)
            .where(self._ready(now))
            .order_by(InboundEvent.received_at)
            .limit(self._settings.messaging_batch_size)
        ).all()

        for candidate in events:
            with operation_lock(
                self._session, f"message:{candidate.external_event_id}"
            ) as acquired:
                if not acquired:
                    continue
                event = self._session.scalar(
                    select(InboundEvent)
                    .where(
                        InboundEvent.id == candidate.id, self._ready(datetime.now(UTC))
                    )
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if event is None:
                    self._session.commit()
                    continue
                await self._process_event(event, result)
        return result

    def _expire_old_messages(
        self, result: MessagingProcessingResult, now: datetime
    ) -> None:
        events = self._session.scalars(
            select(InboundEvent).where(
                self._ready(now),
                InboundEvent.responded_at.is_(None),
            )
        ).all()
        expired = [
            event for event in events if self._response_window_expired(event, now)
        ]
        for candidate in expired:
            with operation_lock(
                self._session, f"message:{candidate.external_event_id}"
            ) as acquired:
                if not acquired:
                    continue
                event = self._session.scalar(
                    select(InboundEvent)
                    .where(
                        InboundEvent.id == candidate.id,
                        self._ready(now),
                    )
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if event is None:
                    self._session.commit()
                    continue
                if (
                    self._response_window_expired(event, now)
                    and event.responded_at is None
                ):
                    self._fail(
                        event, "Message response window expired before processing"
                    )
                    result.failed += 1
                self._session.commit()

    @staticmethod
    def _ready(now: datetime):
        return and_(
            InboundEvent.source == InboundEventSource.MESSAGING,
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

    async def _process_event(
        self, event: InboundEvent, result: MessagingProcessingResult
    ) -> None:
        now = datetime.now(UTC)
        event.status = InboundEventStatus.PROCESSING
        event.attempt_count += 1
        event.last_attempt_at = now
        event.next_retry_at = None
        event.last_error = None
        logger.info(
            "Messaging processing started: message_id=%s attempt=%s",
            event.external_event_id,
            event.attempt_count,
        )

        if event.responded_at is None and self._response_window_expired(event, now):
            self._fail(event, "Message response window expired before processing")
            self._session.commit()
            result.failed += 1
            return

        try:
            if event.decision is None:
                await self._interpreter.interpret(event)
                self._session.commit()

            if event.responded_at is None:
                if self._response_window_expired(event, datetime.now(UTC)):
                    self._fail(
                        event, "Message response window expired during interpretation"
                    )
                    self._session.commit()
                    result.failed += 1
                    return
                await self._respond(event)
                event.responded_at = datetime.now(UTC)
                self._session.commit()

            if event.decision is MessagingDecision.ORDER:
                if event.external_order_id is None:
                    raise InvalidExternalOrderError(
                        "The messaging response has no external order ID"
                    )
                order = await self._ingestion.ingest(
                    event.external_order_id, OrderSource.MESSAGING
                )
                order = await self._order_processing.process_order(order.id)
                event.order = order

            event.status = InboundEventStatus.PROCESSED
            event.processed_at = datetime.now(UTC)
            self._session.commit()
        except MessagingAlreadyRespondedError:
            self._session.rollback()
            event = self._session.get(InboundEvent, event.id)
            event.responded_at = datetime.now(UTC)
            if event.decision is MessagingDecision.DISCARDED:
                event.status = InboundEventStatus.PROCESSED
                event.processed_at = event.responded_at
                result.processed += 1
            else:
                self._fail(
                    event,
                    "Message was already answered; order ID is unavailable. "
                    "Recover with scripts.recover_messaging_order "
                    "and the official OC ID",
                )
                result.failed += 1
            self._session.commit()
            return
        except (MessagingError, PurchaseOrdersError, FarmaCentralError) as exc:
            self._session.rollback()
            event = self._session.get(InboundEvent, event.id)
            retry_after = self._retry_delay(exc)
            self._fail(event, str(exc), retry_after=retry_after)
            self._session.commit()
            result.failed += 1
            result.retry_after_seconds = max(
                result.retry_after_seconds, retry_after or 0
            )
            return
        except (InvalidExternalOrderError, InvalidExternalOrderStateError) as exc:
            self._session.rollback()
            event = self._session.get(InboundEvent, event.id)
            self._fail(event, str(exc))
            self._session.commit()
            result.failed += 1
            return

        result.processed += 1
        logger.info(
            "Messaging event processed: message_id=%s decision=%s order_id=%s",
            event.external_event_id,
            event.decision,
            event.external_order_id,
        )

    async def _respond(self, event: InboundEvent) -> None:
        if event.decision is MessagingDecision.DISCARDED:
            await self._messaging.discard(
                event.external_event_id,
                reason=event.reason or "El mensaje no corresponde a un pedido válido.",
            )
            return
        if not (
            event.interpreted_sku
            and event.interpreted_quantity
            and event.interpreted_delivery_date
        ):
            raise InvalidExternalOrderError(
                "An order decision requires SKU, quantity and delivery date"
            )
        event.external_order_id = await self._messaging.respond_order(
            event.external_event_id,
            sku=event.interpreted_sku,
            quantity=event.interpreted_quantity,
            delivery_date=event.interpreted_delivery_date,
        )

    @staticmethod
    def _response_window_expired(event: InboundEvent, now: datetime) -> bool:
        try:
            message = MessagingWebhookPayload.model_validate(event.payload)
        except ValidationError:
            return False
        return message.received_at + MESSAGE_RESPONSE_WINDOW <= now

    def _retry_delay(
        self, error: MessagingError | PurchaseOrdersError | FarmaCentralError
    ) -> float | None:
        delay = self._settings.messaging_poll_interval_seconds
        if isinstance(
            error, (MessagingHTTPError, PurchaseOrdersHTTPError, FarmaCentralHTTPError)
        ):
            if (
                not isinstance(
                    error,
                    (
                        MessagingAuthenticationError,
                        PurchaseOrdersAuthenticationError,
                        FarmaCentralAuthenticationError,
                    ),
                )
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
            "Messaging event failed: message_id=%s error=%s",
            event.external_event_id,
            error,
        )
