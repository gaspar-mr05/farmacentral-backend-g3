from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import ExternalOrderStatus, Order, OrderItem, OrderSource, OrderStatus
from app.schemas.purchase_orders import PurchaseOrder


class InvalidExternalOrderError(Exception):
    pass


def import_external_order(
    session: Session, detail: PurchaseOrder, *, source: OrderSource, supplier_id: str
) -> Order:
    if detail.supplier_id != supplier_id:
        raise InvalidExternalOrderError("The group is not the supplier of this order")
    if detail.unit_price != detail.unit_price.to_integral_value():
        raise InvalidExternalOrderError(
            "The local sales model requires whole-peso prices"
        )
    unit_price = int(detail.unit_price)
    order_id = session.scalar(
        insert(Order)
        .values(
            external_order_id=detail.id.lower(),
            external_customer_id=detail.customer_id,
            buyer_name=detail.customer_name or detail.customer_id,
            buyer_email=None,
            source=source,
            status=OrderStatus.RECEIVED,
            external_status=ExternalOrderStatus(detail.state),
            delivery_deadline=detail.delivery_deadline,
            total=unit_price * detail.quantity,
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
    return session.scalars(
        select(Order).where(Order.external_order_id == detail.id.lower())
    ).one()
