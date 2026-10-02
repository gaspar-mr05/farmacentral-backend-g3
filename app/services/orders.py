import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clients.market_prices import MarketPriceClient
from app.models import Order, OrderItem, OrderStatus
from app.schemas.orders import OrderCreate
from app.services.catalog import CatalogService


class OrderNotFoundError(Exception):
    pass


class ProductNotAvailableError(Exception):
    pass


class InsufficientStockError(Exception):
    pass


class OrderService:
    def __init__(self, client: MarketPriceClient, session: Session) -> None:
        self._client = client
        self._session = session

    async def create(self, request: OrderCreate) -> Order:
        catalog = {
            item.sku: item
            for item in await CatalogService(self._client, self._session).list_items()
        }

        for requested_item in request.items:
            catalog_item = catalog.get(requested_item.sku)
            if catalog_item is None:
                raise ProductNotAvailableError(
                    f"Product {requested_item.sku} is not available for sale"
                )
            if requested_item.quantity > catalog_item.stock:
                raise InsufficientStockError(
                    f"Insufficient stock for {requested_item.sku}"
                )

        order_items = [
            OrderItem(
                sku=item.sku,
                quantity=item.quantity,
                unit_price=catalog[item.sku].price,
            )
            for item in request.items
        ]
        order = Order(
            buyer_name=request.buyer_name,
            buyer_email=request.buyer_email,
            source=request.source,
            status=OrderStatus.PENDING_PAYMENT,
            total=sum(item.line_total for item in order_items),
            items=order_items,
        )
        self._session.add(order)
        self._session.commit()
        return order


def get_order(session: Session, order_id: uuid.UUID) -> Order:
    statement = (
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    order = session.scalar(statement)
    if order is None:
        raise OrderNotFoundError(f"Order {order_id} was not found")
    return order
