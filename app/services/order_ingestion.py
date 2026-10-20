from sqlalchemy.orm import Session

from app.clients.purchase_orders import PurchaseOrdersClient
from app.db.external_orders import upsert_external_order
from app.models import Order, OrderSource


class OrderIngestionService:
    """Common business pipeline for every external order channel."""

    def __init__(
        self,
        orders: PurchaseOrdersClient,
        session: Session,
        *,
        supplier_id: str,
    ) -> None:
        self._orders = orders
        self._session = session
        self._supplier_id = supplier_id

    async def ingest(self, order_id: str, source: OrderSource) -> Order:
        detail = await self._orders.get_order(order_id)
        return upsert_external_order(
            self._session,
            detail,
            source=source,
            supplier_id=self._supplier_id,
        )
