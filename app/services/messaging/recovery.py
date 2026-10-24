from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.purchase_orders import PurchaseOrdersClient
from app.core.config import Settings, get_settings
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
    Order,
    OrderSource,
)
from app.schemas.messaging import MessagingWebhookPayload
from app.services.orders import OrderIngestionService, OrderProcessingService


class MessagingRecoveryError(Exception):
    pass


class MessagingRecoveryService:
    """Link an operator-identified official OC after a lost messaging response.

    The external API has no message lookup. Matching order contents alone cannot
    establish identity, so an explicit OC ID is required instead of guessing one.
    """

    def __init__(
        self,
        orders: PurchaseOrdersClient,
        session: Session,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._orders = orders
        self._session = session
        self._settings = settings or get_settings()

    async def recover(self, message_id: str, order_id: str) -> Order:
        try:
            event = self._session.scalar(
                select(InboundEvent)
                .where(
                    InboundEvent.source == InboundEventSource.MESSAGING,
                    InboundEvent.external_event_id == message_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if event is None or event.decision is not MessagingDecision.ORDER:
                raise MessagingRecoveryError(
                    "A persisted order interpretation is required"
                )
            if event.external_order_id not in {None, order_id.lower()}:
                raise MessagingRecoveryError(
                    "The message already references another OC"
                )
            if event.status is InboundEventStatus.PROCESSED and event.order is not None:
                order = event.order
                self._session.commit()
                return order
            if event.attempt_count == 0 and event.responded_at is None:
                raise MessagingRecoveryError(
                    "The message has no recorded response attempt"
                )
            message = MessagingWebhookPayload.model_validate(event.payload)
            order = await OrderIngestionService(
                self._orders,
                self._session,
                supplier_id=str(self._settings.farma_central_group),
            ).ingest(order_id, OrderSource.MESSAGING)
            if (
                len(order.items) != 1
                or order.items[0].sku != event.interpreted_sku
                or order.items[0].quantity != event.interpreted_quantity
                or order.delivery_deadline != event.interpreted_delivery_date
                or order.buyer_name != message.sender.name
            ):
                raise MessagingRecoveryError(
                    "The official OC does not match the message"
                )
            other = self._session.scalar(
                select(InboundEvent.id).where(
                    InboundEvent.source == InboundEventSource.MESSAGING,
                    InboundEvent.external_order_id == order.external_order_id,
                    InboundEvent.id != event.id,
                )
            )
            if other is not None:
                raise MessagingRecoveryError(
                    "The OC is already linked to another message"
                )

            order = await OrderProcessingService(
                self._orders, self._session
            ).process_order(order.id)
            event.external_order_id = order.external_order_id
            event.order = order
            event.responded_at = event.responded_at or datetime.now(UTC)
            event.status = InboundEventStatus.PROCESSED
            event.processed_at = datetime.now(UTC)
            event.next_retry_at = None
            event.last_error = None
            self._session.commit()
            return order
        except Exception:
            self._session.rollback()
            raise
