import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.clients.purchase_orders import PurchaseOrdersClient
from app.models import (
    ExternalOrderStatus,
    Location,
    Lot,
    Order,
    OrderItem,
    OrderStatus,
    OrderUnit,
    Product,
    ProductionRun,
    Unit,
)

from .service import OrderNotFoundError
from .state import apply_official_state

logger = logging.getLogger(__name__)


class InvalidExternalOrderStateError(Exception):
    pass


class OrderProcessingService:
    """Accept or reject external orders using a small, conservative policy."""

    def __init__(self, orders: PurchaseOrdersClient, session: Session) -> None:
        self._orders = orders
        self._session = session

    async def process_order(
        self, order_id: uuid.UUID, *, now: datetime | None = None
    ) -> Order:
        now = now or datetime.now(UTC)
        order = self._get_for_update(order_id)
        self._validate_external_order(order)

        if order.external_status in {
            ExternalOrderStatus.EXPIRED,
            ExternalOrderStatus.CANCELLED,
            ExternalOrderStatus.REJECTED,
        }:
            apply_official_state(self._session, order, order.external_status)
            if order.external_status == ExternalOrderStatus.REJECTED:
                self._complete_missing_audit(order, now)
            self._session.flush()
            return order

        if order.status in {
            OrderStatus.ACCEPTED,
            OrderStatus.REJECTED,
            OrderStatus.DISPATCHED,
        }:
            self._complete_missing_audit(order, now)
            return order

        if order.external_status in {
            ExternalOrderStatus.ACCEPTED,
            ExternalOrderStatus.REJECTED,
        }:
            self._reconcile_official_decision(order, now)
            return order

        if order.external_status is not ExternalOrderStatus.CREATED:
            raise InvalidExternalOrderStateError(
                f"External order {order.external_order_id} is "
                f"{order.external_status.value} and cannot be decided"
            )

        accepted, reason = self._decide(order, now)
        external_status = (
            ExternalOrderStatus.ACCEPTED if accepted else ExternalOrderStatus.REJECTED
        )
        await self._orders.update_order_state(
            order.external_order_id, external_status.value
        )
        order.status = OrderStatus.ACCEPTED if accepted else OrderStatus.REJECTED
        order.external_status = external_status
        order.decision_at = now
        order.decision_reason = reason
        self._session.flush()
        logger.info(
            "Order decision recorded: order_id=%s status=%s source=%s",
            order.external_order_id,
            order.status.value,
            order.source.value,
        )
        return order

    def _decide(self, order: Order, now: datetime) -> tuple[bool, str]:
        if order.delivery_deadline is None:
            return False, "Rejected: the order has no delivery deadline"
        if order.delivery_deadline <= now:
            return False, "Rejected: the delivery deadline has passed"
        if not order.items:
            return False, "Rejected: the order has no items"

        requested_by_sku: dict[str, int] = defaultdict(int)
        for item in order.items:
            requested_by_sku[item.sku] += item.quantity

        self._lock_products(requested_by_sku)
        details = []
        can_fulfill = True
        for sku, requested in sorted(requested_by_sku.items()):
            stock = self._available_stock(sku, order.delivery_deadline)
            production = self._confirmed_production(sku, now, order.delivery_deadline)
            committed = self._committed_quantity(sku, order.id)
            capacity = max(stock + production - committed, 0)
            details.append(
                f"{sku}: need {requested}, capacity {capacity} "
                f"(stock {stock}, confirmed production {production}, "
                f"committed {committed})"
            )
            if capacity < requested:
                can_fulfill = False

        prefix = "Accepted" if can_fulfill else "Rejected"
        return can_fulfill, f"{prefix}: {'; '.join(details)}"

    def _lock_products(self, requested_by_sku: dict[str, int]) -> None:
        self._session.scalars(
            select(Product.id)
            .where(Product.sku.in_(sorted(requested_by_sku)))
            .order_by(Product.sku)
            .with_for_update()
        ).all()

    def _available_stock(self, sku: str, deadline: datetime) -> int:
        quantity = self._session.scalar(
            select(func.count(Unit.id))
            .join(Lot, Unit.lot_id == Lot.id)
            .join(Product, Lot.product_id == Product.id)
            .join(Location, Unit.current_location_id == Location.id)
            .where(
                Product.sku == sku,
                Unit.status == "available",
                Unit.effective_expires_at > deadline,
                Location.is_sellable.is_(True),
                ~Unit.order_assignment.has(),
            )
        )
        return int(quantity or 0)

    def _confirmed_production(self, sku: str, now: datetime, deadline: datetime) -> int:
        quantity = self._session.scalar(
            select(func.coalesce(func.sum(ProductionRun.expected_quantity), 0)).where(
                ProductionRun.expected_sku == sku,
                ProductionRun.completed_at.is_(None),
                ProductionRun.available_at >= now,
                ProductionRun.available_at <= deadline,
            )
        )
        return int(quantity or 0)

    def _committed_quantity(self, sku: str, current_order_id: uuid.UUID) -> int:
        assigned = (
            select(func.count(OrderUnit.id))
            .where(OrderUnit.order_item_id == OrderItem.id)
            .correlate(OrderItem)
            .scalar_subquery()
        )
        quantity = self._session.scalar(
            select(
                func.coalesce(
                    func.sum(func.greatest(OrderItem.quantity - assigned, 0)), 0
                )
            )
            .join(Order, OrderItem.order_id == Order.id)
            .where(
                OrderItem.sku == sku,
                Order.id != current_order_id,
                Order.status == OrderStatus.ACCEPTED,
                Order.external_order_id.is_not(None),
            )
        )
        return int(quantity or 0)

    def _get_for_update(self, order_id: uuid.UUID) -> Order:
        order = self._session.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if order is None:
            raise OrderNotFoundError(f"Order {order_id} was not found")
        return order

    @staticmethod
    def _validate_external_order(order: Order) -> None:
        if order.external_order_id is None or order.external_status is None:
            raise InvalidExternalOrderStateError(
                f"Order {order.id} is not an external purchase order"
            )

    @staticmethod
    def _complete_missing_audit(order: Order, now: datetime) -> None:
        if order.decision_at is None:
            order.decision_at = now
        if order.decision_reason is None:
            order.decision_reason = "Existing local decision"

    @staticmethod
    def _reconcile_official_decision(order: Order, now: datetime) -> None:
        accepted = order.external_status is ExternalOrderStatus.ACCEPTED
        order.status = OrderStatus.ACCEPTED if accepted else OrderStatus.REJECTED
        order.decision_at = now
        order.decision_reason = (
            f"Reconciled from official state: {order.external_status.value}"
        )
