import base64
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.clients.purchase_orders import (
    PurchaseOrdersAuthenticationError,
    PurchaseOrdersClient,
    PurchaseOrdersError,
    PurchaseOrdersHTTPError,
)
from app.clients.sftp import SFTPOrdersClient, SFTPOrdersError
from app.core.config import Settings, get_settings
from app.db.external_orders import InvalidExternalOrderError
from app.db.inbound_events import known_sftp_paths, record_sftp_files
from app.models import InboundEvent, InboundEventSource, InboundEventStatus, OrderSource
from app.schemas.sftp_orders import InvalidSFTPOrderError, extract_order_id

from .ingestion import OrderIngestionService
from .processing import (
    InvalidExternalOrderStateError,
    OrderProcessingService,
)

logger = logging.getLogger(__name__)


@dataclass
class SFTPPollResult:
    received: int = 0
    processed: int = 0
    failed: int = 0
    connection_failed: bool = False
    retry_after_seconds: float = 0


class SFTPOrderIngestionService:
    def __init__(
        self,
        sftp: SFTPOrdersClient,
        orders: PurchaseOrdersClient,
        session: Session,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._sftp = sftp
        self._session = session
        self._settings = settings or get_settings()
        self._ingestion = OrderIngestionService(
            orders,
            session,
            supplier_id=str(self._settings.farma_central_group),
        )
        self._processing = OrderProcessingService(orders, session)

    async def poll(self) -> SFTPPollResult:
        result = SFTPPollResult()
        try:
            paths = known_sftp_paths(self._session)
            self._session.commit()
            try:
                files = await self._sftp.fetch_files(skip_paths=paths)
            except SFTPOrdersError as exc:
                result.connection_failed = True
                logger.warning("sFTP poll failed: %s", exc)
            else:
                result.received = record_sftp_files(self._session, files)
                self._session.commit()
            await self._process_pending(result)
            return result
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _ready():
        return and_(
            InboundEvent.source == InboundEventSource.SFTP,
            or_(
                InboundEvent.status == InboundEventStatus.PENDING,
                and_(
                    InboundEvent.status == InboundEventStatus.FAILED,
                    InboundEvent.next_retry_at <= datetime.now(UTC),
                ),
            ),
        )

    async def _process_pending(self, result: SFTPPollResult) -> None:
        event_ids = self._session.scalars(
            select(InboundEvent.id)
            .where(self._ready())
            .order_by(InboundEvent.received_at)
            .limit(self._settings.sftp_batch_size)
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
                    content = base64.b64decode(
                        event.payload["xml_base64"], validate=True
                    )
                except (KeyError, ValueError, TypeError) as exc:
                    raise InvalidSFTPOrderError(
                        "Invalid persisted XML payload"
                    ) from exc
                order_id = extract_order_id(
                    content, max_bytes=self._settings.sftp_max_file_bytes
                )
                event.payload = {**event.payload, "order_id": order_id}
                order = await self._ingestion.ingest(order_id, OrderSource.SFTP)
                order = await self._processing.process_order(order.id)
            except (
                InvalidSFTPOrderError,
                InvalidExternalOrderError,
                InvalidExternalOrderStateError,
            ) as exc:
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
                    "sFTP order imported: path=%s order_id=%s",
                    event.external_event_id,
                    order_id,
                )
            self._session.commit()
            if pause:
                break

    def _retry_delay(self, error: PurchaseOrdersError) -> float | None:
        delay = self._settings.sftp_poll_interval_seconds
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
            "sFTP event failed: path=%s order_id=%s error=%s",
            event.external_event_id,
            event.payload.get("order_id"),
            error,
        )
