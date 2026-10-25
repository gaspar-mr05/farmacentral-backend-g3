from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from app.models import ExternalOrderStatus, Order, OrderItem, OrderSource, OrderStatus
from app.schemas.purchase_orders import PurchaseOrder


class InvalidExternalOrderError(Exception):
    pass


def upsert_external_order(
    session: Session, detail: PurchaseOrder, *, source: OrderSource, supplier_id: str
) -> Order:
    if detail.supplier_id != supplier_id:
        raise InvalidExternalOrderError("The group is not the supplier of this order")
    if detail.unit_price != detail.unit_price.to_integral_value():
        raise InvalidExternalOrderError(
            "The local sales model requires whole-peso prices"
        )
    session.flush()
    unit_price = int(detail.unit_price)
    values = {
        "external_customer_id": detail.customer_id,
        "buyer_name": detail.customer_name or detail.customer_id,
        "buyer_email": None,
        "external_status": ExternalOrderStatus(detail.state),
        "delivery_deadline": detail.delivery_deadline,
        "total": unit_price * detail.quantity,
    }
    order_id = session.scalar(
        insert(Order)
        .values(
            external_order_id=detail.id.lower(),
            source=source,
            status=OrderStatus.RECEIVED,
            **values,
        )
        .on_conflict_do_nothing(constraint="uq_orders_external_order_id")
        .returning(Order.id)
    )
    if order_id is not None:
        session.add(
            OrderItem(
                order_id=order_id,
                sku=detail.sku,
                quantity=detail.quantity,
                unit_price=unit_price,
            )
        )
        session.flush()

    order = session.scalars(
        select(Order)
        .where(Order.external_order_id == detail.id.lower())
        .options(selectinload(Order.items))
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one()
    if order_id is None:
        item = order.items[0]
        if any(item.assigned_units for item in order.items) and (
            item.sku != detail.sku
            or item.quantity != detail.quantity
            or item.unit_price != unit_price
        ):
            raise InvalidExternalOrderError(
                "Cannot rewrite items with reserved or dispatched units"
            )
        for field, value in values.items():
            if field == "external_status" and (
                (
                    value == ExternalOrderStatus.CREATED
                    and order.external_status != ExternalOrderStatus.CREATED
                )
                or (
                    order.external_status == ExternalOrderStatus.FULFILLED
                    and value == ExternalOrderStatus.ACCEPTED
                )
            ):
                continue
            setattr(order, field, value)
        item.sku = detail.sku
        item.quantity = detail.quantity
        item.unit_price = unit_price
    session.flush()
    return order
