from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.dependencies import get_farma_central_client
from app.clients.farma_central_exceptions import FarmaCentralTimeoutError
from app.db.units import upsert_units
from app.main import app
from app.models import CustodyEvent, CustodyEventType, Location, OrderStatus, Unit
from app.schemas.units import UnitData
from tests.api.test_order_fulfillment import _add_order, _add_stock
from tests.support.traceability import create_traceability_scenario


class DispatchClient:
    def __init__(self, units, destination="dispatch"):
        self.spaces = [{"_id": destination, "checkOut": True}]
        self.units = {u.external_unit_id: u for u in units}
        self.positions = {}
        self.moves = []
        self.fail_on = None
        self.timeout_after_move = False
        self.confirm = True
        self.invalid = False

    async def get_spaces(self):
        return self.spaces

    async def get_space_products(self, store_id, sku, *, limit=None):
        if self.invalid:
            return {}
        return [
            {
                "_id": key,
                "sku": sku,
                "store": store_id,
                "expiresAt": unit.effective_expires_at.isoformat(),
            }
            for key, unit in self.units.items()
            if self.positions.get(key) == store_id
        ]

    async def move_product(self, product_id, store_id):
        self.moves.append(product_id)
        if product_id == self.fail_on:
            raise FarmaCentralTimeoutError("Simulated timeout")
        if self.confirm:
            self.positions[product_id] = store_id
        if self.timeout_after_move:
            self.timeout_after_move = False
            raise FarmaCentralTimeoutError("Response lost after moving")


def prepare(session, api_client, quantity=2):
    sku = f"KIT-DISPATCH-{uuid4().hex}"
    units = _add_stock(session, sku=sku, days_until_expiration=[10] * quantity)
    order = _add_order(session, sku=sku, quantity=quantity)
    destination = f"OUT-{uuid4().hex}"
    session.add(
        Location(
            code=destination,
            name="Despacho",
            is_sellable=False,
            is_refrigerated=False,
        )
    )
    session.flush()
    assert api_client.post(f"/api/orders/{order.id}/fulfillment").status_code == 200
    client = DispatchClient(units, destination)
    app.dependency_overrides[get_farma_central_client] = lambda: client
    return order, units, client


def events(session):
    return list(
        session.scalars(
            select(CustodyEvent).where(
                CustodyEvent.event_type == CustodyEventType.DISPATCHED
            )
        )
    )


def test_dispatch_is_idempotent_and_records_delivery(api_client, db_session):
    order, units, client = prepare(db_session, api_client)
    first = api_client.post(f"/api/orders/{order.id}/dispatch")
    second = api_client.post(f"/api/orders/{order.id}/dispatch")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["status"] == "dispatched"
    assert all(a["dispatched_at"] for a in first.json()["items"][0]["assigned_units"])
    assert len(client.moves) == 2
    assert len(events(db_session)) == 2
    assert all(event.order_id == order.id for event in events(db_session))
    assert all(unit.status == "dispatched" for unit in units)
    trace = api_client.get(f"/api/traceability/{units[0].lot_id}").json()
    assert len(trace["deliveries"]) == 2
    assert trace["deliveries"][0]["buyer_email"] == order.buyer_email
    assert api_client.post(f"/api/orders/{order.id}/payments").status_code == 409
    assert api_client.post(f"/api/orders/{order.id}/fulfillment").status_code == 409


def test_dispatch_resumes_partial_progress(api_client, db_session):
    order, units, client = prepare(db_session, api_client)
    second_id = order.items[0].assigned_units[1].external_unit_id
    client.fail_on = second_id
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 503
    assert len(events(db_session)) == 1
    assert db_session.get(type(order), order.id).status == OrderStatus.PAID
    client.fail_on = None
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 200
    assert len(events(db_session)) == 2
    assert client.moves.count(order.items[0].assigned_units[0].external_unit_id) == 1


def test_timeout_after_external_move_does_not_repeat_move(api_client, db_session):
    order, units, client = prepare(db_session, api_client, quantity=1)
    client.timeout_after_move = True
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 503
    assert not events(db_session)
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 200
    assert len(client.moves) == 1
    assert len(events(db_session)) == 1


@pytest.mark.parametrize(
    "problem",
    ["unpaid", "incomplete", "expired", "no_destination", "ambiguous_destination"],
)
def test_dispatch_rejects_invalid_state(api_client, db_session, problem):
    order, units, client = prepare(db_session, api_client, quantity=1)
    if problem == "unpaid":
        order.status = OrderStatus.PENDING_PAYMENT
    elif problem == "incomplete":
        order.items[0].quantity = 2
    elif problem == "expired":
        units[0].effective_expires_at = datetime.now(UTC)
    elif problem == "no_destination":
        client.spaces = []
    else:
        client.spaces.append({"_id": "other", "checkOut": True})
    db_session.commit()
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 409
    assert not client.moves
    assert not events(db_session)


@pytest.mark.parametrize("problem", ["unconfirmed", "invalid_response"])
def test_dispatch_requires_external_confirmation(api_client, db_session, problem):
    order, units, client = prepare(db_session, api_client, quantity=1)
    client.confirm = problem != "unconfirmed"
    client.invalid = problem == "invalid_response"
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 502
    assert not events(db_session)
    assert units[0].status == "reserved"


def test_unknown_order_returns_404(api_client, db_session):
    app.dependency_overrides[get_farma_central_client] = lambda: DispatchClient([])
    assert api_client.post(f"/api/orders/{uuid4()}/dispatch").status_code == 404


def test_sync_preserves_dispatched_unit(api_client, db_session):
    order, units, client = prepare(db_session, api_client, quantity=1)
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 200
    unit = units[0]
    upsert_units(
        db_session,
        [
            UnitData(
                unit.external_unit_id,
                unit.lot.external_lot_id,
                unit.current_location.code,
                "available",
                unit.effective_expires_at,
            )
        ],
        {unit.lot.external_lot_id: unit.lot},
        {unit.current_location.code: unit.current_location},
    )
    assert unit.status == "dispatched"
    upsert_units(db_session, [], {}, {})
    assert unit.status == "dispatched"


def test_raw_lot_traceability_reaches_customer(api_client, db_session):
    scenario = create_traceability_scenario(db_session)
    kit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == scenario.kit_unit_external_id)
    )
    order = _add_order(db_session, sku=kit.lot.product.sku, quantity=1)
    destination = f"OUT-{uuid4().hex}"
    db_session.add(
        Location(
            code=destination,
            name="Despacho",
            is_sellable=False,
        )
    )
    db_session.flush()
    assert api_client.post(f"/api/orders/{order.id}/fulfillment").status_code == 200
    app.dependency_overrides[get_farma_central_client] = lambda: DispatchClient(
        [kit], destination
    )
    assert api_client.post(f"/api/orders/{order.id}/dispatch").status_code == 200
    response = api_client.get(f"/api/traceability/{scenario.raw_lot_id}")
    delivery = response.json()["deliveries"][0]
    assert delivery["lot_id"] == str(scenario.kit_lot_id)
    assert delivery["order_id"] == str(order.id)
    assert delivery["quantity"] == 1


def test_concurrent_dispatch_records_one_delivery(db_session):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.db.base import Base
    from app.services.order_dispatch import OrderDispatchService
    from app.services.order_fulfillment import OrderFulfillmentService

    # Separate schema and committed transactions exercise real PostgreSQL locks.
    original_engine = db_session.get_bind().engine
    schema = f"dispatch_test_{uuid4().hex}"
    with original_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        original_engine.url, connect_args={"options": f"-csearch_path={schema}"}
    )
    entered = Event()
    release = Event()
    second_started = Event()
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            sku = f"KIT-CONCURRENT-{uuid4().hex}"
            units = _add_stock(session, sku=sku, days_until_expiration=[10])
            order = _add_order(session, sku=sku, quantity=1)
            session.add(Location(code="dispatch", name="Despacho"))
            session.commit()
            OrderFulfillmentService(session).fulfill(order.id)
            order_id = order.id
            client = DispatchClient(units)

        original_move = client.move_product

        async def blocked_move(product_id, store_id):
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
            await original_move(product_id, store_id)

        client.move_product = blocked_move

        def dispatch(second=False):
            with Session(engine) as session:
                if second:
                    second_started.set()
                return asyncio.run(
                    OrderDispatchService(client, session).dispatch(order_id)
                ).status

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(dispatch)
            assert entered.wait(5)
            second = executor.submit(dispatch, True)
            assert second_started.wait(5)
            release.set()
            assert first.result(timeout=10) == OrderStatus.DISPATCHED
            assert second.result(timeout=10) == OrderStatus.DISPATCHED
        assert len(client.moves) == 1
        with Session(engine) as session:
            assert len(events(session)) == 1
    finally:
        release.set()
        engine.dispose()
        with original_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
