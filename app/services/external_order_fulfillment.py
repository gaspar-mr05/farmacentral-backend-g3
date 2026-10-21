import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import FarmaCentralError
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.purchase_orders_exceptions import PurchaseOrdersError
from app.core.config import Settings, get_settings
from app.models import ExternalOrderStatus, Order, OrderStatus
from app.services.order_dispatch import InvalidDispatchStateError, OrderDispatchService
from app.services.order_fulfillment import (
    InsufficientFulfillmentStockError,
    InvalidFulfillmentStateError,
    OrderFulfillmentService,
    OrderNotPaidError,
)

logger = logging.getLogger(__name__)


@dataclass
class ExternalOrderFulfillmentResult:
    completed: int = 0
    pending: int = 0
    failed: int = 0


class ExternalOrderFulfillmentService:
    def __init__(
        self,
        farma_central: FarmaCentralClient,
        purchase_orders: PurchaseOrdersClient,
        session: Session,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._farma_central = farma_central
        self._purchase_orders = purchase_orders
        self._session = session
        self._settings = settings or get_settings()

    async def process_pending(self) -> ExternalOrderFulfillmentResult:
        result = ExternalOrderFulfillmentResult()
        order_ids = self._session.scalars(
            select(Order.id)
            .where(
                Order.external_order_id.is_not(None),
                Order.external_status == ExternalOrderStatus.ACCEPTED,
                Order.status.in_([OrderStatus.ACCEPTED, OrderStatus.DISPATCHED]),
            )
            .order_by(Order.delivery_deadline, Order.created_at)
            .limit(self._settings.external_order_batch_size)
        ).all()

        for order_id in order_ids:
            try:
                order = self._session.get(Order, order_id)
                if order is None or order.external_order_id is None:
                    continue
                if order.delivery_deadline and order.delivery_deadline <= datetime.now(
                    UTC
                ):
                    await self._refresh_official_state(order)
                    if order.external_status == ExternalOrderStatus.FULFILLED:
                        result.completed += 1
                    else:
                        result.pending += 1
                    continue

                if order.status == OrderStatus.ACCEPTED:
                    OrderFulfillmentService(self._session).fulfill(order_id)
                    await OrderDispatchService(
                        self._farma_central, self._session
                    ).dispatch(order_id)

                order = self._session.get(Order, order_id)
                await self._refresh_official_state(order)
                if order.external_status == ExternalOrderStatus.FULFILLED:
                    result.completed += 1
                else:
                    result.pending += 1
            except InsufficientFulfillmentStockError:
                self._session.rollback()
                result.pending += 1
            except (
                FarmaCentralError,
                PurchaseOrdersError,
                InvalidDispatchStateError,
                InvalidFulfillmentStateError,
                OrderNotPaidError,
            ) as exc:
                self._session.rollback()
                result.failed += 1
                logger.warning(
                    "External order fulfillment failed: order_id=%s error=%s",
                    order_id,
                    exc,
                )
        return result

    async def _refresh_official_state(self, order: Order) -> None:
        detail = await self._purchase_orders.get_order(order.external_order_id)
        order.external_status = ExternalOrderStatus(detail.state)
        if order.external_status == ExternalOrderStatus.EXPIRED:
            order.status = OrderStatus.EXPIRED
        self._session.commit()
