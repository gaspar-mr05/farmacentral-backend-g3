from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models import ExternalOrderStatus, Location, OrderItem, OrderStatus
from app.services.orders import ExternalOrderFulfillmentService
from tests.api.test_order_dispatch import DispatchClient, DispatchOrders
from tests.api.test_order_fulfillment import _add_stock
from tests.support.farma_central import make_settings
from tests.support.orders import make_external_order, make_purchase_order


class FakePurchaseOrders:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def get_order(self, order_id: str):
        self.fetched.append(order_id)
        return make_purchase_order(
            id=order_id,
            estado="cumplida",
            cantidad=1,
            despachado=1,
        )


def add_accepted_order(
    db_session,
    sku: str,
    external_id: str,
    deadline: datetime,
    *,
    quantity: int = 1,
):
    order = make_external_order(
        external_order_id=external_id,
        status=OrderStatus.ACCEPTED,
        external_status=ExternalOrderStatus.ACCEPTED,
        delivery_deadline=deadline,
        items=[OrderItem(sku=sku, quantity=quantity, unit_price=1500)],
    )
    db_session.add(order)
    db_session.flush()
    return order


@pytest.mark.anyio
async def test_fulfills_external_orders_by_nearest_deadline(db_session):
    now = datetime.now(UTC)
    first_sku = f"KIT-EARLY-{uuid4().hex}"
    second_sku = f"KIT-LATE-{uuid4().hex}"
    first_units = _add_stock(db_session, sku=first_sku, days_until_expiration=[10])
    second_units = _add_stock(db_session, sku=second_sku, days_until_expiration=[10])
    late = add_accepted_order(
        db_session,
        second_sku,
        "222222222222222222222222",
        now + timedelta(hours=2),
    )
    early = add_accepted_order(
        db_session,
        first_sku,
        "111111111111111111111111",
        now + timedelta(hours=1),
    )
    destination = f"OUT-{uuid4().hex}"
    db_session.add(Location(code=destination, name="Despacho", is_sellable=False))
    db_session.commit()
    farma_central = DispatchClient(first_units + second_units, destination)
    purchase_orders = DispatchOrders(farma_central)
    settings: Settings = make_settings(external_order_batch_size=100)

    result = await ExternalOrderFulfillmentService(
        farma_central,
        purchase_orders,
        db_session,
        settings=settings,
    ).process_pending()

    assert result.completed == 2
    assert result.pending == result.failed == 0
    assert list(dict.fromkeys(purchase_orders.fetched)) == [
        early.external_order_id,
        late.external_order_id,
    ]
    assert db_session.get(type(early), early.id).status == OrderStatus.DISPATCHED
    assert db_session.get(type(late), late.id).status == OrderStatus.DISPATCHED
    assert early.external_status == ExternalOrderStatus.FULFILLED
    assert late.external_status == ExternalOrderStatus.FULFILLED
    assert len(farma_central.dispatches) == 2


@pytest.mark.anyio
async def test_keeps_order_pending_until_stock_is_available(db_session):
    order = add_accepted_order(
        db_session,
        f"KIT-NOT-READY-{uuid4().hex}",
        "333333333333333333333333",
        datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.commit()
    purchase_orders = FakePurchaseOrders()

    result = await ExternalOrderFulfillmentService(
        DispatchClient([]),
        purchase_orders,
        db_session,
        settings=make_settings(),
    ).process_pending()

    assert result.pending == 1
    assert result.completed == result.failed == 0
    assert purchase_orders.fetched == []
    assert db_session.get(type(order), order.id).status == OrderStatus.ACCEPTED


@pytest.mark.anyio
async def test_resumes_after_a_partial_external_dispatch(db_session):
    sku = f"KIT-PARTIAL-{uuid4().hex}"
    units = _add_stock(db_session, sku=sku, days_until_expiration=[10, 10])
    order = add_accepted_order(
        db_session,
        sku,
        "444444444444444444444444",
        datetime.now(UTC) + timedelta(hours=1),
        quantity=2,
    )
    destination = f"OUT-{uuid4().hex}"
    db_session.add(Location(code=destination, name="Despacho", is_sellable=False))
    db_session.commit()
    farma_central = DispatchClient(units, destination)
    farma_central.fail_on = units[1].external_unit_id
    purchase_orders = DispatchOrders(farma_central, quantity=2)
    service = ExternalOrderFulfillmentService(
        farma_central,
        purchase_orders,
        db_session,
        settings=make_settings(),
    )

    first = await service.process_pending()
    farma_central.fail_on = None
    second = await service.process_pending()

    assert first.failed == 1
    assert second.completed == 1
    assert farma_central.dispatches == [
        (units[0].external_unit_id, order.external_order_id),
        (units[1].external_unit_id, order.external_order_id),
    ]
    assert farma_central.moves.count(units[0].external_unit_id) == 1


@pytest.mark.anyio
async def test_worker_rejects_persisted_undecided_order_and_does_not_decide_twice(
    db_session,
):
    order = make_external_order()
    db_session.add(order)
    db_session.commit()
    official = AsyncMock()
    official.get_order.return_value = make_purchase_order(
        vencimiento=order.delivery_deadline
    )
    service = ExternalOrderFulfillmentService(
        DispatchClient([]), official, db_session, settings=make_settings()
    )

    first = await service.process_pending()
    second = await service.process_pending()

    assert first.pending == 1
    assert second.pending == second.failed == second.completed == 0
    assert order.status is OrderStatus.REJECTED
    assert order.decision_at is not None
    official.update_order_state.assert_awaited_once_with(
        order.external_order_id, "rechazada"
    )


@pytest.mark.anyio
async def test_worker_accepts_and_fulfills_a_persisted_undecided_order(db_session):
    sku = f"KIT-UNDECIDED-{uuid4().hex}"
    units = _add_stock(db_session, sku=sku, days_until_expiration=[10])
    order = make_external_order(items=[OrderItem(sku=sku, quantity=1, unit_price=1500)])
    destination = f"OUT-{uuid4().hex}"
    db_session.add_all(
        [order, Location(code=destination, name="Despacho", is_sellable=False)]
    )
    db_session.commit()
    farma = DispatchClient(units, destination)

    class UndecidedOrders(DispatchOrders):
        state = "creada"

        async def get_order(self, order_id):
            detail = await super().get_order(order_id)
            return detail.model_copy(
                update={"state": detail.state if self.client.dispatches else self.state}
            )

        async def update_order_state(self, order_id, state):
            self.state = ExternalOrderStatus(state)

    official = UndecidedOrders(farma)
    official.state = ExternalOrderStatus.CREATED
    result = await ExternalOrderFulfillmentService(
        farma, official, db_session, settings=make_settings()
    ).process_pending()

    assert result.completed == 1
    assert order.status is OrderStatus.DISPATCHED
    assert farma.dispatches == [(units[0].external_unit_id, order.external_order_id)]
