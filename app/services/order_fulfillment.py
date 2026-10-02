import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Location,
    Lot,
    Order,
    OrderItem,
    OrderStatus,
    OrderUnit,
    Product,
    Unit,
)
from app.services.orders import OrderNotFoundError


class OrderNotPaidError(Exception):
    pass


class InsufficientFulfillmentStockError(Exception):
    pass


class InvalidFulfillmentStateError(Exception):
    pass


class OrderFulfillmentService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def fulfill(self, order_id: uuid.UUID) -> Order:
        try:
            order = self._get_order_for_update(order_id)
            if order.status != OrderStatus.PAID:
                raise OrderNotPaidError(
                    f"Order {order.id} must be paid before fulfillment"
                )

            for item in order.items:
                self._assign_item(item)

            self._session.commit()
            return self._get_order(order_id)
        except Exception:
            self._session.rollback()
            raise

    def _assign_item(self, item: OrderItem) -> None:
        assigned_count = len(item.assigned_units)
        if assigned_count > item.quantity:
            raise InvalidFulfillmentStateError(
                f"Order item {item.id} has more units than requested"
            )

        units_needed = item.quantity - assigned_count
        if units_needed == 0:
            return

        units = list(
            self._session.scalars(
                select(Unit)
                .join(Lot, Unit.lot_id == Lot.id)
                .join(Product, Lot.product_id == Product.id)
                .join(Location, Unit.current_location_id == Location.id)
                .where(
                    Product.sku == item.sku,
                    Unit.status == "available",
                    Unit.effective_expires_at > datetime.now(UTC),
                    Location.is_sellable.is_(True),
                    ~exists().where(OrderUnit.unit_id == Unit.id),
                )
                .order_by(Unit.effective_expires_at, Unit.id)
                .limit(units_needed)
                .with_for_update(of=Unit, skip_locked=True)
            )
        )
        if len(units) != units_needed:
            raise InsufficientFulfillmentStockError(
                f"Insufficient stock to fulfill {item.sku}"
            )

        for unit in units:
            unit.status = "reserved"
            item.assigned_units.append(OrderUnit(unit=unit))

    def _get_order_for_update(self, order_id: uuid.UUID) -> Order:
        order = self._session.scalar(
            select(Order)
            .options(
                selectinload(Order.items)
                .selectinload(OrderItem.assigned_units)
                .selectinload(OrderUnit.unit)
            )
            .where(Order.id == order_id)
            .with_for_update()
        )
        if order is None:
            raise OrderNotFoundError(f"Order {order_id} was not found")
        return order

    def _get_order(self, order_id: uuid.UUID) -> Order:
        order = self._session.scalar(
            select(Order)
            .options(
                selectinload(Order.items)
                .selectinload(OrderItem.assigned_units)
                .selectinload(OrderUnit.unit)
            )
            .where(Order.id == order_id)
        )
        if order is None:
            raise OrderNotFoundError(f"Order {order_id} was not found")
        return order
