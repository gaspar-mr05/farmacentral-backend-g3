from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.db.external_orders import InvalidExternalOrderError
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
    Order,
)
from app.services.messaging.recovery import (
    MessagingRecoveryError,
    MessagingRecoveryService,
)
from tests.services.test_messaging_processing import add_event
from tests.support.farma_central import make_settings
from tests.support.orders import EXTERNAL_ORDER_ID, make_purchase_order


def recovery_setup(session):
    event = add_event(session, decision=MessagingDecision.ORDER)
    event.status = InboundEventStatus.FAILED
    event.attempt_count = 1
    event.last_error = "Message was already answered; order ID is unavailable"
    session.commit()
    official = AsyncMock()
    official.get_order.return_value = make_purchase_order(
        sku=event.interpreted_sku,
        cantidad=event.interpreted_quantity,
        vencimiento=event.interpreted_delivery_date,
        nombreCliente=event.payload["sender"]["name"],
    )
    return event, official


@pytest.mark.anyio
async def test_explicit_recovery_links_and_decides_existing_oc_once(db_session):
    event, official = recovery_setup(db_session)
    service = MessagingRecoveryService(official, db_session, settings=make_settings())

    order = await service.recover(event.external_event_id, EXTERNAL_ORDER_ID)
    again = await service.recover(event.external_event_id, EXTERNAL_ORDER_ID)

    assert again.id == order.id
    assert event.order_id == order.id
    assert event.external_order_id == EXTERNAL_ORDER_ID
    assert event.status is InboundEventStatus.PROCESSED
    assert event.last_error is event.next_retry_at is None
    assert event.responded_at is not None
    official.get_order.assert_awaited_once()
    official.update_order_state.assert_awaited_once_with(EXTERNAL_ORDER_ID, "rechazada")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field", ["sku", "quantity", "delivery_deadline", "buyer_name"]
)
async def test_recovery_rejects_mismatching_oc_without_persisting_or_deciding(
    db_session, field
):
    event, official = recovery_setup(db_session)
    updates = {
        "sku": {"sku": "OTHER-KIT"},
        "quantity": {"quantity": 7},
        "delivery_deadline": {
            "delivery_deadline": event.interpreted_delivery_date + timedelta(hours=1)
        },
        "buyer_name": {"customer_name": "Another customer"},
    }
    official.get_order.return_value = official.get_order.return_value.model_copy(
        update=updates[field]
    )

    with pytest.raises(MessagingRecoveryError, match="does not match"):
        await MessagingRecoveryService(
            official, db_session, settings=make_settings()
        ).recover(event.external_event_id, EXTERNAL_ORDER_ID)

    official.update_order_state.assert_not_awaited()
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0
    assert event.external_order_id is None
    assert event.status is InboundEventStatus.FAILED


@pytest.mark.anyio
async def test_recovery_does_not_reassign_a_message_with_another_oc(db_session):
    event, official = recovery_setup(db_session)
    event.external_order_id = "111111111111111111111111"
    db_session.commit()

    with pytest.raises(MessagingRecoveryError, match="another OC"):
        await MessagingRecoveryService(
            official, db_session, settings=make_settings()
        ).recover(event.external_event_id, EXTERNAL_ORDER_ID)

    official.get_order.assert_not_awaited()


@pytest.mark.anyio
async def test_recovery_rejects_an_oc_owned_by_another_supplier(db_session):
    event, official = recovery_setup(db_session)
    official.get_order.return_value = official.get_order.return_value.model_copy(
        update={"supplier_id": "4"}
    )

    with pytest.raises(InvalidExternalOrderError, match="not the supplier"):
        await MessagingRecoveryService(
            official, db_session, settings=make_settings()
        ).recover(event.external_event_id, EXTERNAL_ORDER_ID)

    official.update_order_state.assert_not_awaited()
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.anyio
async def test_recovery_cannot_use_an_oc_linked_to_another_message(db_session):
    event, official = recovery_setup(db_session)
    db_session.add(
        InboundEvent(
            source=InboundEventSource.MESSAGING,
            external_event_id="other-message",
            external_order_id=EXTERNAL_ORDER_ID,
            payload={},
        )
    )
    db_session.commit()

    with pytest.raises(MessagingRecoveryError, match="another message"):
        await MessagingRecoveryService(
            official, db_session, settings=make_settings()
        ).recover(event.external_event_id, EXTERNAL_ORDER_ID)

    official.update_order_state.assert_not_awaited()
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0
