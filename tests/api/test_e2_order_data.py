from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import OrderStatus, OrderUnit, Unit
from tests.support.orders import make_external_order
from tests.support.traceability import create_traceability_scenario


def test_external_order_details_can_be_read_without_email(api_client, db_session):
    order = make_external_order()
    db_session.add(order)
    db_session.flush()

    response = api_client.get(f"/api/orders/{order.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["external_order_id"] == order.external_order_id
    assert data["external_customer_id"] == order.external_customer_id
    assert data["buyer_email"] is None
    assert data["source"] == "sftp"
    assert data["status"] == "received"
    assert data["external_status"] == "creada"
    assert datetime.fromisoformat(data["delivery_deadline"]) == order.delivery_deadline


@pytest.mark.parametrize("source", ["sftp", "pubsub", "messaging"])
def test_web_checkout_cannot_impersonate_an_external_channel(api_client, source):
    response = api_client.post(
        "/api/orders",
        json={
            "buyer_name": "Cliente web",
            "buyer_email": "cliente@example.com",
            "source": source,
            "items": [{"sku": "KIT-ANALGESIA", "quantity": 1}],
        },
    )

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "source"] for error in response.json()["detail"]
    )


def test_traceability_can_show_external_delivery_without_email(api_client, db_session):
    scenario = create_traceability_scenario(db_session)
    unit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == scenario.kit_unit_external_id)
    )
    order = make_external_order(status=OrderStatus.DISPATCHED)
    db_session.add(order)
    db_session.flush()
    db_session.add(
        OrderUnit(
            order_item_id=order.items[0].id,
            unit_id=unit.id,
            dispatched_at=datetime.now(UTC),
        )
    )
    unit.status = "dispatched"
    db_session.flush()

    response = api_client.get(f"/api/traceability/{scenario.kit_lot_id}")

    assert response.status_code == 200
    delivery = response.json()["deliveries"][0]
    assert delivery["order_id"] == str(order.id)
    assert delivery["buyer_name"] == order.buyer_name
    assert delivery["buyer_email"] is None
