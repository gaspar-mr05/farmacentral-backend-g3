import pytest

from app.schemas.pubsub_orders import InvalidPubSubOrderError, extract_order_id

ORDER_ID = "64abc123def456789012abcd"


@pytest.mark.parametrize(
    "data",
    [
        ORDER_ID.encode(),
        f'{{"orderId": "{ORDER_ID.upper()}"}}'.encode(),
        f'{{"id": "{ORDER_ID}", "cliente": "999", "sku": "KIT-PEDIA-FEBRIL"}}'.encode(),
    ],
)
def test_extracts_order_id_from_supported_pubsub_payloads(data):
    assert extract_order_id(data) == ORDER_ID


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not-an-order-id",
        b"\xff",
        b"{",
        b"{}",
        b'{"orderId": 123}',
    ],
)
def test_rejects_invalid_pubsub_payloads(data):
    with pytest.raises(InvalidPubSubOrderError):
        extract_order_id(data)
