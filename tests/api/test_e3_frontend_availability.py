from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.models import CustodyEvent, CustodyEventType, MarketRole, Recall, Unit
from app.services.recalls import RecallInvestigationService
from tests.api.test_orders_payments import (
    FakeCheckoutClient,
    _add_sellable_kit,
    _override_checkout,
    _override_market_prices,
)
from tests.support.billing import make_period, make_trade
from tests.support.traceability import create_traceability_scenario


@pytest.mark.parametrize("status", ["recall_pending", "quarantined", "expired"])
def test_traceability_keeps_blocked_physical_units_visible(
    api_client, db_session, status
):
    scenario = create_traceability_scenario(db_session)
    unit = db_session.scalar(select(Unit).where(Unit.lot_id == scenario.kit_lot_id))
    unit.status = status
    db_session.commit()
    response = api_client.get(f"/api/traceability/{scenario.kit_lot_id}")
    assert response.status_code == 200
    assert (
        response.json()["current_units"][0]["external_unit_id"] == unit.external_unit_id
    )
    assert response.json()["current_units"][0]["status"] == status


def test_purchase_traceability_and_recalls_do_not_call_e3_services(
    api_client, db_session, monkeypatch
):
    blocked = AsyncMock(
        side_effect=AssertionError("No SOAP, trades or LLM requests in the public flow")
    )
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    scenario = create_traceability_scenario(db_session)
    period = make_period()
    db_session.add(period)
    db_session.flush()
    raw = db_session.scalar(select(Unit).where(Unit.lot_id == scenario.raw_lot_id))
    kit = db_session.scalar(select(Unit).where(Unit.lot_id == scenario.kit_lot_id))
    kit.status = "dispatched"
    bought = make_trade(period.id, sku=raw.lot.product.sku, quantity=1)
    sold = make_trade(
        period.id,
        role=MarketRole.SELLER,
        sku=kit.lot.product.sku,
        quantity=1,
        counterparty_id="5",
        external_offer_id="offer-2",
        external_order_id="2" * 24,
    )
    db_session.add_all([bought, sold])
    db_session.flush()
    db_session.add_all(
        [
            CustodyEvent(
                unit_id=raw.id,
                market_trade_id=bought.id,
                event_type=CustodyEventType.RECEIVED,
            ),
            CustodyEvent(
                unit_id=kit.id,
                market_trade_id=sold.id,
                event_type=CustodyEventType.DISPATCHED,
            ),
        ]
    )
    now = datetime.now(UTC)
    recall = Recall(
        recall_id="rcl-public-availability",
        batch=raw.lot.external_lot_id,
        sku=raw.lot.product.sku,
        reason="Quality",
        issued_at=now,
        deadline=now + timedelta(minutes=30),
    )
    db_session.add(recall)
    db_session.commit()
    RecallInvestigationService(db_session).investigate(recall.recall_id)
    available = _add_sellable_kit(db_session)
    _override_market_prices(db_session, {available.sku: 1750})
    checkout = FakeCheckoutClient()
    _override_checkout(checkout)

    assert api_client.get("/api/catalog").status_code == 200
    created = api_client.post(
        "/api/orders",
        json={
            "buyer_name": "Client",
            "buyer_email": "client@example.test",
            "items": [{"sku": available.sku, "quantity": 2}],
        },
    )
    assert created.status_code == 201 and created.json()["total"] == 3500
    payment = api_client.post(f"/api/orders/{created.json()['id']}/payments")
    assert payment.status_code == 201 and payment.json()["amount"] == 3500
    trace = api_client.get(f"/api/traceability/{scenario.kit_lot_id}")
    assert trace.status_code == 200
    transfers = trace.json()["market_transfers"]
    assert {row["role"] for row in transfers} == {"buyer", "seller"}
    by_order = {row["external_order_id"]: row for row in transfers}
    assert (
        by_order[bought.external_order_id]["external_unit_id"] == raw.external_unit_id
    )
    assert by_order[bought.external_order_id]["lot_id"] == str(scenario.raw_lot_id)
    assert by_order[sold.external_order_id]["counterparty_id"] == "5"
    assert [row["order_id"] for row in trace.json()["deliveries"]] == [
        sold.external_order_id
    ]
    assert api_client.get("/api/recalls").status_code == 200
    detail = api_client.get(f"/api/recalls/{recall.recall_id}")
    assert detail.status_code == 200
    assert (
        detail.json()["market_deliveries"][0]["external_order_id"]
        == sold.external_order_id
    )
    blocked.assert_not_awaited()
