import pytest
from sqlalchemy import func, select

from app.clients.pubsub_orders import PubSubOrderMessage, PubSubOrdersError
from app.models import InboundEvent, InboundEventStatus, Order, OrderSource
from app.services.pubsub_orders import PubSubOrderIngestionService
from tests.support.farma_central import make_settings
from tests.support.orders import EXTERNAL_ORDER_ID, make_purchase_order


def message(message_id="message-1", order_id=EXTERNAL_ORDER_ID):
    return PubSubOrderMessage(
        message_id=message_id,
        ack_id=f"ack-{message_id}",
        data=f'{{"orderId": "{order_id}"}}'.encode(),
    )


class FakePubSub:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.acknowledged = []
        self.ack_error = None

    async def pull(self):
        return self.messages

    async def acknowledge(self, ack_ids):
        if self.ack_error:
            raise self.ack_error
        self.acknowledged.extend(ack_ids)


class FakeOrders:
    def __init__(self):
        self.requested_ids = []
        self.decisions = []
        self.states = {}

    async def get_order(self, order_id):
        self.requested_ids.append(order_id)
        return make_purchase_order(
            id=order_id, estado=self.states.get(order_id, "creada")
        )

    async def update_order_state(self, order_id, state):
        self.decisions.append((order_id, state))
        self.states[order_id] = state


def make_service(session, pubsub, orders):
    return PubSubOrderIngestionService(
        pubsub,
        orders,
        session,
        settings=make_settings(
            pubsub_topic_id="topic", pubsub_subscription_id="subscription"
        ),
    )


@pytest.mark.anyio
async def test_persists_acknowledges_and_imports_official_order_once(db_session):
    pubsub = FakePubSub(message())
    orders = FakeOrders()
    service = make_service(db_session, pubsub, orders)

    first = await service.poll()
    second = await service.poll()

    assert (first.received, first.processed, first.failed) == (1, 1, 0)
    assert (second.received, second.processed, second.failed) == (0, 0, 0)
    assert pubsub.acknowledged == ["ack-message-1", "ack-message-1"]
    assert orders.requested_ids == [EXTERNAL_ORDER_ID]
    order = db_session.scalars(select(Order)).one()
    assert order.source is OrderSource.PUBSUB
    assert order.items[0].sku == "KIT-ANALGESIA"
    event = db_session.scalars(select(InboundEvent)).one()
    assert event.external_event_id == "message-1"
    assert event.order_id == order.id
    assert event.status is InboundEventStatus.PROCESSED


@pytest.mark.anyio
async def test_two_notifications_for_one_order_create_one_local_order(db_session):
    pubsub = FakePubSub(message("message-1"), message("message-2"))
    result = await make_service(db_session, pubsub, FakeOrders()).poll()

    assert result.processed == 2
    assert db_session.scalar(select(func.count()).select_from(Order)) == 1
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 2


@pytest.mark.anyio
async def test_ack_failure_keeps_the_durable_event_and_processes_it(db_session):
    pubsub = FakePubSub(message())
    pubsub.ack_error = PubSubOrdersError("temporary failure")

    result = await make_service(db_session, pubsub, FakeOrders()).poll()

    assert result.acknowledgement_failed
    assert result.processed == 1
    event = db_session.scalars(select(InboundEvent)).one()
    assert event.status is InboundEventStatus.PROCESSED
    assert event.order_id is not None


@pytest.mark.anyio
async def test_invalid_message_does_not_stop_a_valid_message(db_session):
    invalid = PubSubOrderMessage("bad", "ack-bad", b"invalid")
    result = await make_service(
        db_session, FakePubSub(invalid, message()), FakeOrders()
    ).poll()

    assert (result.processed, result.failed) == (1, 1)
    events = {
        event.external_event_id: event
        for event in db_session.scalars(select(InboundEvent))
    }
    assert events["bad"].status is InboundEventStatus.FAILED
    assert events["bad"].next_retry_at is None
