import json

from pydantic import TypeAdapter, ValidationError

from app.schemas.purchase_orders import PurchaseOrderId

ORDER_ID_ADAPTER = TypeAdapter(PurchaseOrderId)


class InvalidPubSubOrderError(Exception):
    pass


def extract_order_id(data: bytes) -> str:
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise InvalidPubSubOrderError("Pub/Sub data is not UTF-8") from exc

    candidate = text
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            candidate = payload["orderId"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise InvalidPubSubOrderError("Pub/Sub data must contain orderId") from exc

    try:
        return ORDER_ID_ADAPTER.validate_python(candidate).lower()
    except ValidationError as exc:
        raise InvalidPubSubOrderError(
            "Pub/Sub data contains an invalid order ID"
        ) from exc
