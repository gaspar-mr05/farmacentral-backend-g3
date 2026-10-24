import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient, FarmaCentralError
from app.clients.purchase_orders import PurchaseOrdersClient, PurchaseOrdersError
from app.core.config import Settings, get_settings
from app.models import ExternalOrderStatus, Order, OrderItem, OrderStatus, OrderUnit

from .dispatch import InvalidDispatchStateError, OrderDispatchService
from .fulfillment import (
    InsufficientFulfillmentStockError,
    InvalidFulfillmentStateError,
    OrderFulfillmentService,
    OrderNotPaidError,
)
from .state import apply_official_state

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
        unresolved_dispatch = exists(
            select(OrderUnit.id)
            .join(OrderItem, OrderUnit.order_item_id == OrderItem.id)
            .where(
                OrderItem.order_id == Order.id,
                OrderUnit.dispatched_at.is_(None),
                OrderUnit.dispatch_expected_quantity.is_not(None),
            )
        )
        pending_assignment = exists(
            select(OrderUnit.id)
            .join(OrderItem, OrderUnit.order_item_id == OrderItem.id)
            .where(OrderItem.order_id == Order.id, OrderUnit.dispatched_at.is_(None))
        )
        order_ids = self._session.scalars(
            select(Order.id)
            .where(
                Order.external_order_id.is_not(None),
                or_(
                    Order.status == OrderStatus.ACCEPTED,
                    (Order.status == OrderStatus.DISPATCHED)
                    & (Order.external_status == ExternalOrderStatus.ACCEPTED),
                    pending_assignment,
                ),
            )
            .order_by(Order.delivery_deadline, Order.created_at)
            .limit(self._settings.external_order_batch_size)
        ).all()

        for order_id in order_ids:
            try:
                order = self._session.get(Order, order_id)
                if order is None or order.external_order_id is None:
                    continue
                if order.external_status != ExternalOrderStatus.ACCEPTED or (
                    order.delivery_deadline
                    and order.delivery_deadline <= datetime.now(UTC)
                ):
                    await self._refresh_official_state(order)
                    if self._session.scalar(
                        select(unresolved_dispatch)
                        .select_from(Order)
                        .where(Order.id == order.id)
                    ):
                        await OrderDispatchService(
                            self._farma_central, self._session, self._purchase_orders
                        ).dispatch(order_id, reconcile_only=True)
                    if order.external_status == ExternalOrderStatus.FULFILLED:
                        if order.status != OrderStatus.DISPATCHED:
                            raise InvalidDispatchStateError(
                                "Official fulfillment has no confirmed local deliveries"
                            )
                        result.completed += 1
                    else:
                        result.pending += 1
                    continue

                if order.status == OrderStatus.ACCEPTED:
                    OrderFulfillmentService(self._session).fulfill(order_id)
                    await OrderDispatchService(
                        self._farma_central, self._session, self._purchase_orders
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
        order = self._session.scalars(
            select(Order)
            .where(Order.id == order.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one()
        detail = await self._purchase_orders.get_order(order.external_order_id)
        apply_official_state(self._session, order, ExternalOrderStatus(detail.state))
        self._session.commit()
