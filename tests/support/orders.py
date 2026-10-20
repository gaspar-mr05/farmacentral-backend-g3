from datetime import UTC, datetime, timedelta

from app.models import ExternalOrderStatus, Order, OrderItem, OrderSource, OrderStatus
from app.schemas.purchase_orders import PurchaseOrder

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


def make_purchase_order(**overrides):
    values = {
        "id": EXTERNAL_ORDER_ID,
        "cliente": "farmacia-externa",
        "proveedor": "3",
        "sku": "KIT-ANALGESIA",
        "cantidad": 2,
        "despachado": 0,
        "precioUnitario": 1500,
        "nombreCliente": "Farmacia externa",
        "estado": "creada",
        "historial": [{"nombre": "creada", "fecha": "2026-10-20T12:00:00Z"}],
        "creada": "2026-10-20T12:00:00Z",
        "actualizada": "2026-10-20T12:00:00Z",
        "vencimiento": "2026-10-20T14:00:00Z",
        "facturado": False,
    }
    values.update(overrides)
    return PurchaseOrder.model_validate(values)
