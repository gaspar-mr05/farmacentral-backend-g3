from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.clients.farma_central import FarmaCentralTimeoutError
from app.db.external_orders import upsert_external_order
from app.models import (
    ExternalOrderStatus,
    Location,
    OrderSource,
    OrderStatus,
    OrderUnit,
    Unit,
)
from app.services.orders import (
    ExternalOrderFulfillmentService,
    InvalidDispatchStateError,
    OrderDispatchService,
    OrderFulfillmentService,
    OrderProcessingService,
)
from tests.api.test_order_dispatch import DispatchClient, DispatchOrders, events
from tests.api.test_order_fulfillment import _add_stock
from tests.services.test_external_order_fulfillment import add_accepted_order
from tests.services.test_order_processing import FakeOrders, add_order
from tests.support.farma_central import make_settings
from tests.support.orders import make_purchase_order


def prepare_dispatch(session, quantity=1):
    sku = f"KIT-RECOVERY-{uuid4().hex}"
    units = _add_stock(session, sku=sku, days_until_expiration=[10] * quantity)
    order = add_accepted_order(
        session,
        sku,
        "111111111111111111111111",
        datetime.now(UTC) + timedelta(hours=1),
        quantity=quantity,
    )
    destination = f"OUT-{uuid4().hex}"
    session.add(Location(code=destination, name="Dispatch", is_sellable=False))
    session.commit()
    client = DispatchClient(units, destination)
    official = DispatchOrders(client, quantity)
    return order, units, client, official


@pytest.mark.anyio
@pytest.mark.parametrize("reserved", [1, 2])
async def test_reserved_units_are_not_subtracted_twice(db_session, reserved):
    sku = f"KIT-CAPACITY-{uuid4().hex}"
    _add_stock(db_session, sku=sku, days_until_expiration=[10] * 5)
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=1)
    first = add_accepted_order(
        db_session,
        sku,
        "111111111111111111111111",
        deadline,
        quantity=2,
    )
    OrderFulfillmentService(db_session).fulfill(first.id)
    if reserved == 1:
        assignment = first.items[0].assigned_units.pop()
        assignment.unit.status = "available"
        db_session.flush()
    second = add_order(db_session, sku, 3, deadline, "222222222222222222222222")
    official = FakeOrders()

    result = await OrderProcessingService(official, db_session).process_order(
        second.id, now=now
    )

    assert result.status == OrderStatus.ACCEPTED
    assert f"committed {2 - reserved}" in result.decision_reason
    assert official.decisions == [(second.external_order_id, "aceptada")]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "state,local_status",
    [
        ("vencida", OrderStatus.EXPIRED),
        ("anulada", OrderStatus.CANCELLED),
        ("cumplida", OrderStatus.ACCEPTED),
    ],
)
async def test_duplicate_notification_preserves_official_state(
    db_session, state, local_status
):
    order, units, _, _ = prepare_dispatch(db_session)
    OrderFulfillmentService(db_session).fulfill(order.id)
    decided_at = datetime.now(UTC) - timedelta(minutes=10)
    order.decision_at = decided_at
    order.decision_reason = "Original decision"
    upsert_external_order(
        db_session,
        make_purchase_order(
            id=order.external_order_id, estado=state, sku=order.items[0].sku, cantidad=1
        ),
        source=OrderSource.PUBSUB,
        supplier_id="3",
    )
    official = FakeOrders()

    await OrderProcessingService(official, db_session).process_order(order.id)
    db_session.flush()

    assert order.external_status == ExternalOrderStatus(state)
    assert order.status == local_status
    assert order.decision_at == decided_at
    assert order.decision_reason == "Original decision"
    assert official.decisions == []
    if state != "cumplida":
        assert units[0].status == "available"
        assert order.items[0].assigned_units == []


@pytest.mark.anyio
@pytest.mark.parametrize("already_expired", [False, True])
async def test_expiration_releases_only_undelivered_reservations(
    db_session, already_expired
):
    order, units, client, _ = prepare_dispatch(db_session, quantity=2)
    OrderFulfillmentService(db_session).fulfill(order.id)
    delivered, reserved = order.items[0].assigned_units
    delivered.dispatched_at = datetime.now(UTC)
    delivered.unit.status = "dispatched"
    order.delivery_deadline = datetime.now(UTC) - timedelta(seconds=1)
    if already_expired:
        order.external_status = ExternalOrderStatus.EXPIRED
        order.status = OrderStatus.EXPIRED
    db_session.commit()

    class ExpiredOrders:
        async def get_order(self, order_id):
            return make_purchase_order(id=order_id, estado="vencida", despachado=1)

    result = await ExternalOrderFulfillmentService(
        client,
        ExpiredOrders(),
        db_session,
        settings=make_settings(),
    ).process_pending()
    db_session.expire_all()

    assert result.failed == 0
    assert order.status == OrderStatus.EXPIRED
    assert delivered.unit.status == "dispatched"
    assert db_session.get(Unit, reserved.unit_id).status == "available"
    assert db_session.scalar(select(func.count(OrderUnit.id))) == 1
    assert client.dispatches == []


@pytest.mark.anyio
async def test_legacy_fulfilled_order_without_intent_is_not_falsely_completed(
    db_session,
):
    order, _, client, _ = prepare_dispatch(db_session)
    OrderFulfillmentService(db_session).fulfill(order.id)
    order.external_status = ExternalOrderStatus.FULFILLED
    db_session.commit()

    class FulfilledOrders:
        async def get_order(self, order_id):
            return make_purchase_order(
                id=order_id, estado="cumplida", despachado=1, cantidad=1
            )

    result = await ExternalOrderFulfillmentService(
        client,
        FulfilledOrders(),
        db_session,
        settings=make_settings(),
    ).process_pending()

    assert result.completed == 0
    assert result.failed == 1
    assert client.dispatches == events(db_session) == []
    assert order.items[0].assigned_units[0].dispatched_at is None


@pytest.mark.anyio
@pytest.mark.parametrize("quantity", [1, 2])
async def test_worker_recovers_lost_dispatch_response_without_redelivery(
    db_session, quantity
):
    order, units, client, official = prepare_dispatch(db_session, quantity)
    client.timeout_after_dispatch = True

    first = await ExternalOrderFulfillmentService(
        client,
        official,
        db_session,
        settings=make_settings(),
    ).process_pending()
    db_session.expire_all()
    pending = db_session.scalars(
        select(OrderUnit).where(
            OrderUnit.dispatch_expected_quantity.is_not(None),
        )
    ).one()
    assert first.failed == 1
    assert pending.dispatch_expected_quantity == 1
    assert pending.dispatched_at is None
    assert len(events(db_session)) == 0

    second = await ExternalOrderFulfillmentService(
        client,
        official,
        db_session,
        settings=make_settings(),
    ).process_pending()
    third = await ExternalOrderFulfillmentService(
        client,
        official,
        db_session,
        settings=make_settings(),
    ).process_pending()

    assert second.completed == 1
    assert second.failed == third.failed == third.completed == 0
    assert order.status == OrderStatus.DISPATCHED
    assert order.external_status == ExternalOrderStatus.FULFILLED
    assert len(client.dispatches) == quantity
    assert len({unit_id for unit_id, _ in client.dispatches}) == quantity
    assert len(events(db_session)) == quantity
    assert all(unit.status == "dispatched" for unit in units)
    assert all(
        a.dispatched_at and a.dispatch_expected_quantity is None
        for a in order.items[0].assigned_units
    )


@pytest.mark.anyio
async def test_dispatch_intent_can_resume_when_request_never_reached_server(db_session):
    order, _, client, official = prepare_dispatch(db_session)

    async def fail_before_dispatch(product_id, order_id):
        raise FarmaCentralTimeoutError("Request never reached the server")

    send = client.dispatch_product
    client.dispatch_product = fail_before_dispatch
    service = ExternalOrderFulfillmentService(
        client, official, db_session, settings=make_settings()
    )
    assert (await service.process_pending()).failed == 1
    client.dispatch_product = send

    assert (await service.process_pending()).completed == 1
    assert len(client.dispatches) == len(events(db_session)) == 1
    assert len(client.moves) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("counter_advanced", [False, True])
async def test_ambiguous_dispatch_does_not_invent_delivery_or_retry(
    db_session, counter_advanced
):
    order, units, client, official = prepare_dispatch(db_session)
    OrderFulfillmentService(db_session).fulfill(order.id)
    assignment = order.items[0].assigned_units[0]
    assignment.dispatch_expected_quantity = 1
    if counter_advanced:
        client.dispatches.append((units[0].external_unit_id, order.external_order_id))
        client.positions[units[0].external_unit_id] = client.spaces[0]["_id"]
    db_session.commit()

    with pytest.raises(InvalidDispatchStateError, match="cannot be reconciled"):
        await OrderDispatchService(client, db_session, official).dispatch(order.id)

    assert len(client.dispatches) == int(counter_advanced)
    assert events(db_session) == []
    assert assignment.dispatched_at is None
    assert assignment.dispatch_expected_quantity == 1


@pytest.mark.anyio
@pytest.mark.parametrize("dispatched", [False, True])
async def test_expiration_reconciles_intent_before_releasing_stock(
    db_session, dispatched
):
    order, units, client, official = prepare_dispatch(db_session, quantity=2)
    client.timeout_after_dispatch = dispatched
    if not dispatched:

        async def fail(product_id, order_id):
            raise FarmaCentralTimeoutError("No response")

        client.dispatch_product = fail
    service = ExternalOrderFulfillmentService(
        client, official, db_session, settings=make_settings()
    )
    assert (await service.process_pending()).failed == 1
    order.delivery_deadline = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    class ExpiredOrders:
        async def get_order(self, order_id):
            return make_purchase_order(
                id=order_id,
                estado="vencida",
                despachado=int(dispatched),
            )

    result = await ExternalOrderFulfillmentService(
        client,
        ExpiredOrders(),
        db_session,
        settings=make_settings(),
    ).process_pending()

    assert result.failed == 0
    assert order.status == OrderStatus.EXPIRED
    assert len(events(db_session)) == int(dispatched)
    assert len(order.items[0].assigned_units) == int(dispatched)
    assert sum(u.status == "reserved" for u in units) == 0
    assert len(client.dispatches) == int(dispatched)
