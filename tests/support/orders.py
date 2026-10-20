from datetime import UTC, datetime, timedelta

from app.models import ExternalOrderStatus, Order, OrderItem, OrderSource, OrderStatus

EXTERNAL_ORDER_ID = "64abc123def456789012abcd"


def make_external_order(**overrides):
    values = {
        "buyer_name": "Farmacia externa",
        "buyer_email": None,
        "external_order_id": EXTERNAL_ORDER_ID,
        "external_customer_id": "farmacia-externa",
        "source": OrderSource.SFTP,
        "status": OrderStatus.RECEIVED,
        "external_status": ExternalOrderStatus.CREATED,
        "delivery_deadline": datetime.now(UTC) + timedelta(hours=2),
        "total": 3000,
        "items": [OrderItem(sku="KIT-ANALGESIA", quantity=2, unit_price=1500)],
    }
    values.update(overrides)
    return Order(**values)
