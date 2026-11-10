from decimal import Decimal

import pytest

from app.db.external_orders import InvalidExternalOrderError, upsert_external_order
from app.models import OrderSource
from tests.api.test_orders_payments import (
    FakeCheckoutClient,
    _add_order,
    _override_checkout,
)
from tests.support.orders import make_purchase_order


@pytest.mark.parametrize(
    "source",
    [OrderSource.SFTP, OrderSource.PUBSUB, OrderSource.MESSAGING, OrderSource.MARKET],
)
def test_external_order_prices_are_exact_in_storage_and_json(
    api_client, db_session, source
):
    detail = make_purchase_order(precioUnitario="1.123456789", cantidad=3)
    order = upsert_external_order(db_session, detail, source=source, supplier_id="3")
    db_session.flush()
    db_session.expire_all()

    assert order.total == Decimal("3.370370367")
    assert order.items[0].unit_price == Decimal("1.123456789")
    response = api_client.get(f"/api/orders/{order.id}")
    assert response.status_code == 200
    assert response.json()["total"] == "3.370370367"
    assert response.json()["items"][0]["unit_price"] == "1.123456789"
    assert response.json()["items"][0]["line_total"] == "3.370370367"


def test_existing_whole_peso_orders_and_checkout_keep_integer_json(
    api_client, db_session
):
    order = _add_order(db_session)
    db_session.expire_all()  # Load NUMERIC values from PostgreSQL, not Python ints.
    assert isinstance(order.total, Decimal)
    checkout = FakeCheckoutClient(external_status="SUCCESS")
    _override_checkout(checkout)

    response = api_client.get(f"/api/orders/{order.id}")
    payment = api_client.post(f"/api/orders/{order.id}/payments")

    assert response.status_code == 200
    assert type(response.json()["total"]) is int
    assert type(response.json()["items"][0]["unit_price"]) is int
    assert payment.status_code == 201
    assert type(payment.json()["amount"]) is int
    assert type(checkout.initialize_requests[0]["amount"]) is int
    assert checkout.initialize_requests[0]["amount"] == 3000


def test_fractional_orders_cannot_be_silently_truncated_for_checkout(
    api_client, db_session
):
    detail = make_purchase_order(precioUnitario="1.25", cantidad=1)
    order = upsert_external_order(
        db_session, detail, source=OrderSource.MARKET, supplier_id="3"
    )
    db_session.flush()
    checkout = FakeCheckoutClient(external_status="SUCCESS")
    _override_checkout(checkout)

    response = api_client.post(f"/api/orders/{order.id}/payments")

    assert response.status_code == 409
    assert "whole-peso" in response.json()["detail"]
    assert checkout.initialize_requests == []
    assert order.total == Decimal("1.25")


def test_buyer_market_order_is_not_imported_as_a_sale(db_session):
    detail = make_purchase_order(proveedor="4", cliente="3", precioUnitario="1.25")
    with pytest.raises(InvalidExternalOrderError, match="not the supplier"):
        upsert_external_order(
            db_session, detail, source=OrderSource.MARKET, supplier_id="3"
        )
