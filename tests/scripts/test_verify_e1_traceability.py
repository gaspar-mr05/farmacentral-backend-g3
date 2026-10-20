from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.dependencies import get_farma_central_client
from app.main import app
from app.models import CustodyEvent, CustodyEventType, Location, Unit
from scripts.verify_e1_traceability import verify_delivery
from tests.api.test_order_dispatch import DispatchClient
from tests.api.test_order_fulfillment import _add_order
from tests.support.traceability import create_traceability_scenario


@pytest.fixture
def recorded_delivery(api_client, db_session):
    scenario = create_traceability_scenario(db_session)
    unit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == scenario.kit_unit_external_id)
    )
    destination = f"VERIFY-OUT-{uuid4().hex}"
    db_session.add_all(
        [
            Location(code=destination, name="Despacho", is_sellable=False),
            CustodyEvent(
                unit_id=unit.id,
                event_type=CustodyEventType.PRODUCED,
                to_location_id=unit.current_location_id,
            ),
        ]
    )
    order = _add_order(db_session, sku=unit.lot.product.sku, quantity=1)
    db_session.flush()
    app.dependency_overrides[get_farma_central_client] = lambda: DispatchClient(
        [unit], destination
    )
    assert api_client.post(f"/api/orders/{order.id}/fulfillment").status_code == 200
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 200
    db_session.expire_all()
    return unit, order, scenario


def test_verification_reconstructs_delivery_from_input_lots(
    db_session, recorded_delivery
):
    unit, order, scenario = recorded_delivery

    result = verify_delivery(db_session, unit.external_unit_id)

    assert result["order_id"] == str(order.id)
    assert result["customer"] == order.buyer_name
    assert set(result["ancestor_lots"]) == {
        scenario.raw_lot_external_id,
        scenario.intermediate_lot_external_id,
    }
    assert [event["event"] for event in result["custody"]] == [
        "produced",
        "moved",
        "dispatched",
    ]
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


@pytest.mark.parametrize("missing", ["lineage", "dispatch_event"])
def test_verification_detects_incomplete_delivery_history(
    db_session, recorded_delivery, missing
):
    unit, _, _ = recorded_delivery
    if missing == "lineage":
        unit.lot.produced_by.output_lot_id = None
        expected = "no recorded input lots"
    else:
        event = next(
            event
            for event in unit.custody_events
            if event.event_type == CustodyEventType.DISPATCHED
        )
        db_session.delete(event)
        expected = "no dispatch custody event"
    db_session.flush()
    db_session.expire_all()

    with pytest.raises(ValueError, match=expected):
        verify_delivery(db_session, unit.external_unit_id)
