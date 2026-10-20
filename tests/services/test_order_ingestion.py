import pytest
from sqlalchemy import func, select

from app.models import Order, OrderItem, OrderSource
from app.services.order_ingestion import OrderIngestionService
from tests.support.orders import EXTERNAL_ORDER_ID, make_purchase_order


class FakeOrders:
    def __init__(self, detail):
        self.detail = detail
        self.requested_ids = []

    async def get_order(self, order_id):
        self.requested_ids.append(order_id)
        return self.detail


@pytest.mark.anyio
async def test_repeated_order_updates_one_shared_local_order(db_session):
    client = FakeOrders(make_purchase_order())
    service = OrderIngestionService(client, db_session, supplier_id="3")

    first = await service.ingest(EXTERNAL_ORDER_ID, OrderSource.SFTP)
    db_session.commit()
    client.detail = make_purchase_order(
        nombreCliente="Farmacia actualizada",
        sku="KIT-TRAUMA",
        cantidad=3,
        precioUnitario=2000,
        vencimiento="2026-10-21T14:00:00Z",
    )
    second = await service.ingest(EXTERNAL_ORDER_ID, OrderSource.PUBSUB)
    db_session.commit()

    assert second.id == first.id
    assert second.source is OrderSource.SFTP
    assert second.buyer_name == "Farmacia actualizada"
    assert second.total == 6000
    assert second.items[0].sku == "KIT-TRAUMA"
    assert second.items[0].quantity == 3
    assert second.items[0].unit_price == 2000
    assert client.requested_ids == [EXTERNAL_ORDER_ID, EXTERNAL_ORDER_ID]
    assert db_session.scalar(select(func.count()).select_from(Order)) == 1
    assert db_session.scalar(select(func.count()).select_from(OrderItem)) == 1
