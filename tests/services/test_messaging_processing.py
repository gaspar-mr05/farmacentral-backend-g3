from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.clients.farma_central import FarmaCentralClient, FarmaCentralTimeoutError
from app.clients.llm_gateway import LLMGatewayClient
from app.clients.messaging import (
    MessagingAlreadyRespondedError,
    MessagingHTTPError,
    MessagingTimeoutError,
)
from app.clients.purchase_orders import PurchaseOrdersConnectionError
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
    Order,
    OrderSource,
    Product,
    ProductCategory,
)
from app.services.messaging import (
    MessageInterpretationService,
    MessagingProcessingService,
)
from tests.support.farma_central import catalog_product, make_settings
from tests.support.orders import EXTERNAL_ORDER_ID, make_purchase_order

NOW = datetime.now(UTC)
DEADLINE = NOW + timedelta(hours=2)


class FakeMessaging:
    def __init__(self):
        self.orders = []
        self.discards = []
        self.error = None

    async def respond_order(self, message_id, **interpretation):
        self.orders.append((message_id, interpretation))
        if self.error:
            raise self.error
        return EXTERNAL_ORDER_ID

    async def discard(self, message_id, *, reason):
        self.discards.append((message_id, reason))
        if self.error:
            raise self.error


class FakeInterpreter:
    def __init__(self):
        self.calls = 0

    async def interpret(self, event):
        self.calls += 1
        event.decision = MessagingDecision.DISCARDED
        event.reason = "No corresponde a un pedido"
        return event


class FakeOrders:
    def __init__(self):
        self.requested = []
        self.decisions = []
        self.error = None

    async def get_order(self, order_id):
        self.requested.append(order_id)
        if self.error:
            raise self.error
        return make_purchase_order(
            id=order_id,
            sku="KIT-RESP-ADULTO",
            cantidad=2,
            vencimiento=DEADLINE,
        )

    async def update_order_state(self, order_id, state):
        self.decisions.append((order_id, state))


def add_event(db_session, *, decision=None, received_at=NOW):
    event = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id="msg-worker",
        payload={
            "messageId": "msg-worker",
            "sender": {"name": "Farmacia", "phone": "+56911111111"},
            "text": "Necesito dos kits para hoy a las 19:00",
            "receivedAt": received_at.isoformat(),
        },
        decision=decision,
    )
    if decision is MessagingDecision.ORDER:
        event.interpreted_sku = "KIT-RESP-ADULTO"
        event.interpreted_quantity = 2
        event.interpreted_delivery_date = DEADLINE
    elif decision is MessagingDecision.DISCARDED:
        event.reason = "No corresponde a un pedido"
    db_session.add(event)
    db_session.commit()
    return event


def make_service(db_session, messaging=None, interpreter=None, orders=None):
    messaging = messaging or FakeMessaging()
    interpreter = interpreter or FakeInterpreter()
    orders = orders or FakeOrders()
    service = MessagingProcessingService(
        messaging,
        interpreter,
        orders,
        db_session,
        settings=make_settings(messaging_batch_size=10),
    )
    return service, messaging, interpreter, orders


@pytest.mark.anyio
async def test_order_is_registered_imported_and_decided(db_session):
    incoming = add_event(db_session, decision=MessagingDecision.ORDER)
    service, messaging, interpreter, orders = make_service(db_session)

    result = await service.process_pending()
    db_session.refresh(incoming)

    assert (result.processed, result.failed) == (1, 0)
    assert interpreter.calls == 0
    assert messaging.orders == [
        (
            "msg-worker",
            {
                "sku": "KIT-RESP-ADULTO",
                "quantity": 2,
                "delivery_date": DEADLINE,
            },
        )
    ]
    assert incoming.responded_at is not None
    assert incoming.external_order_id == EXTERNAL_ORDER_ID
    assert incoming.status is InboundEventStatus.PROCESSED
    assert incoming.processed_at is not None
    order = db_session.scalars(select(Order)).one()
    assert incoming.order_id == order.id
    assert order.source is OrderSource.MESSAGING
    assert orders.decisions == [(EXTERNAL_ORDER_ID, "rechazada")]


@pytest.mark.anyio
async def test_discard_is_answered_without_creating_an_order(db_session):
    incoming = add_event(db_session, decision=MessagingDecision.DISCARDED)
    service, messaging, interpreter, orders = make_service(db_session)

    result = await service.process_pending()
    db_session.refresh(incoming)

    assert (result.processed, result.failed) == (1, 0)
    assert messaging.discards == [("msg-worker", "No corresponde a un pedido")]
    assert not messaging.orders
    assert not orders.requested
    assert interpreter.calls == 0
    assert incoming.status is InboundEventStatus.PROCESSED
    assert incoming.responded_at is not None
    assert db_session.scalars(select(Order)).all() == []


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["timeout", "invalid", "missing-component"])
async def test_catalog_failure_is_retryable_without_llm_call_or_message_response(
    db_session, failure
):
    db_session.add(
        Product(
            sku="KIT-TEST",
            name="Kit",
            category=ProductCategory.KIT,
            batch_size=1,
        )
    )
    incoming = add_event(db_session)
    catalog = AsyncMock(spec=FarmaCentralClient)
    if failure == "timeout":
        catalog.get_available_products.side_effect = FarmaCentralTimeoutError("timeout")
    else:
        catalog.get_available_products.return_value = (
            {}
            if failure == "invalid"
            else [
                catalog_product(
                    "KIT-TEST", "Kit", sellable=True, components=("MISSING",)
                ),
            ]
        )
    gateway = AsyncMock(spec=LLMGatewayClient)
    interpreter = MessageInterpretationService(
        gateway, db_session, catalog_client=catalog
    )
    service, messaging, _, _ = make_service(db_session, interpreter=interpreter)

    result = await service.process_pending()
    db_session.refresh(incoming)

    assert result.failed == 1 and result.processed == 0
    assert incoming.status is InboundEventStatus.FAILED
    assert incoming.next_retry_at is not None
    assert incoming.decision is None
    assert incoming.responded_at is None
    assert incoming.external_order_id is None
    gateway.propose_order.assert_not_awaited()
    assert messaging.orders == [] and messaging.discards == []


@pytest.mark.anyio
async def test_pending_message_is_interpreted_before_responding(db_session):
    incoming = add_event(db_session)
    service, messaging, interpreter, _ = make_service(db_session)

    await service.process_pending()
    db_session.refresh(incoming)

    assert interpreter.calls == 1
    assert messaging.discards
    assert incoming.decision is MessagingDecision.DISCARDED
    assert incoming.status is InboundEventStatus.PROCESSED


@pytest.mark.anyio
async def test_temporary_messaging_failure_is_retried(db_session):
    incoming = add_event(db_session, decision=MessagingDecision.DISCARDED)
    messaging = FakeMessaging()
    messaging.error = MessagingTimeoutError("temporary")
    service, _, _, _ = make_service(db_session, messaging=messaging)

    first = await service.process_pending()
    db_session.refresh(incoming)

    assert (first.processed, first.failed) == (0, 1)
    assert incoming.status is InboundEventStatus.FAILED
    assert incoming.next_retry_at is not None
    assert incoming.responded_at is None

    incoming.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    messaging.error = None
    second = await service.process_pending()
    db_session.refresh(incoming)

    assert (second.processed, second.failed) == (1, 0)
    assert incoming.status is InboundEventStatus.PROCESSED
    assert len(messaging.discards) == 2


@pytest.mark.anyio
async def test_rate_limit_retry_after_is_respected(db_session):
    incoming = add_event(db_session, decision=MessagingDecision.DISCARDED)
    messaging = FakeMessaging()
    messaging.error = MessagingHTTPError(429, retry_after_seconds=30)
    service, _, _, _ = make_service(db_session, messaging=messaging)

    before = datetime.now(UTC)
    result = await service.process_pending()
    db_session.refresh(incoming)

    assert result.retry_after_seconds == 30
    assert incoming.next_retry_at >= before + timedelta(seconds=30)


@pytest.mark.anyio
async def test_expired_response_is_not_retried(db_session):
    incoming = add_event(db_session, decision=MessagingDecision.DISCARDED)
    messaging = FakeMessaging()
    messaging.error = MessagingHTTPError(410)
    service, _, _, _ = make_service(db_session, messaging=messaging)

    result = await service.process_pending()
    db_session.refresh(incoming)

    assert result.failed == 1
    assert incoming.status is InboundEventStatus.FAILED
    assert incoming.next_retry_at is None


@pytest.mark.anyio
async def test_old_message_is_failed_without_calling_the_llm_or_messaging_api(
    db_session,
):
    incoming = add_event(
        db_session,
        received_at=datetime.now(UTC) - timedelta(minutes=31),
    )
    service, messaging, interpreter, orders = make_service(db_session)

    result = await service.process_pending()
    db_session.refresh(incoming)

    assert (result.processed, result.failed) == (0, 1)
    assert interpreter.calls == 0
    assert messaging.orders == []
    assert messaging.discards == []
    assert orders.requested == []
    assert incoming.status is InboundEventStatus.FAILED
    assert incoming.decision is None
    assert incoming.next_retry_at is None
    assert "window expired" in incoming.last_error


@pytest.mark.anyio
async def test_expired_backlog_does_not_block_a_current_message(db_session):
    first = add_event(
        db_session,
        received_at=datetime.now(UTC) - timedelta(minutes=32),
    )
    first.external_event_id = "msg-old-1"
    second = add_event(
        db_session,
        received_at=datetime.now(UTC) - timedelta(minutes=31),
    )
    second.external_event_id = "msg-old-2"
    current = add_event(db_session)
    current.external_event_id = "msg-current"
    db_session.commit()
    service, messaging, interpreter, _ = make_service(db_session)
    service._settings.messaging_batch_size = 1

    result = await service.process_pending()
    db_session.expire_all()

    assert (result.processed, result.failed) == (1, 2)
    assert db_session.get(InboundEvent, first.id).status is InboundEventStatus.FAILED
    assert db_session.get(InboundEvent, second.id).status is InboundEventStatus.FAILED
    assert (
        db_session.get(InboundEvent, current.id).status is InboundEventStatus.PROCESSED
    )
    assert interpreter.calls == 1
    assert messaging.discards == [("msg-current", "No corresponde a un pedido")]


@pytest.mark.anyio
async def test_response_is_not_repeated_when_order_import_temporarily_fails(db_session):
    incoming = add_event(db_session, decision=MessagingDecision.ORDER)
    messaging = FakeMessaging()
    orders = FakeOrders()
    orders.error = PurchaseOrdersConnectionError("temporary")
    service, _, _, _ = make_service(db_session, messaging=messaging, orders=orders)

    first = await service.process_pending()
    db_session.refresh(incoming)

    assert first.failed == 1
    assert incoming.responded_at is not None
    assert incoming.external_order_id == EXTERNAL_ORDER_ID
    assert incoming.next_retry_at is not None

    incoming.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    orders.error = None
    second = await service.process_pending()
    db_session.refresh(incoming)

    assert second.processed == 1
    assert len(messaging.orders) == 1
    assert incoming.status is InboundEventStatus.PROCESSED


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        (MessagingDecision.DISCARDED, InboundEventStatus.PROCESSED),
        (MessagingDecision.ORDER, InboundEventStatus.FAILED),
    ],
)
async def test_already_answered_message_stops_retrying(
    db_session, decision, expected_status
):
    incoming = add_event(db_session, decision=decision)
    messaging = FakeMessaging()
    messaging.error = MessagingAlreadyRespondedError(409)
    service, _, _, _ = make_service(db_session, messaging=messaging)

    await service.process_pending()
    db_session.refresh(incoming)

    assert incoming.status is expected_status
    assert incoming.responded_at is not None
    assert incoming.next_retry_at is None


@pytest.mark.anyio
async def test_stale_processing_event_is_recovered_after_restart(db_session):
    incoming = add_event(db_session, decision=MessagingDecision.DISCARDED)
    incoming.status = InboundEventStatus.PROCESSING
    incoming.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
    db_session.commit()
    service, messaging, _, _ = make_service(db_session)

    result = await service.process_pending()

    assert result.processed == 1
    assert messaging.discards
