from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
)
from tests.support.orders import make_external_order


@pytest.mark.parametrize("source", list(InboundEventSource))
def test_event_is_recorded_before_order_exists(db_session, source):
    event = InboundEvent(
        source=source, external_event_id="event-1", payload={"reference": "event-1"}
    )
    db_session.add(event)
    db_session.flush()
    db_session.expire_all()

    saved = db_session.get(InboundEvent, event.id)
    assert saved.source is source
    assert saved.received_at.utcoffset().total_seconds() == 0
    assert saved.status is InboundEventStatus.PENDING
    assert saved.attempt_count == 0
    assert saved.payload == {"reference": "event-1"}
    assert saved.order_id is None
    assert saved.processed_at is None


def test_repeated_event_is_rejected_without_overwriting_original_payload(db_session):
    event = InboundEvent(
        source=InboundEventSource.PUBSUB,
        external_event_id="message-1",
        payload={"orderId": "original"},
    )
    db_session.add(event)
    db_session.flush()

    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(
            InboundEvent(
                source=event.source,
                external_event_id=event.external_event_id,
                payload={"orderId": "changed"},
            )
        )
        db_session.flush()

    assert (
        error.value.orig.diag.constraint_name == "uq_inbound_events_source_external_id"
    )
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 1
    db_session.refresh(event)
    assert event.payload == {"orderId": "original"}


def test_same_reference_from_different_channels_can_link_to_one_order(db_session):
    events = [
        InboundEvent(source=source, external_event_id="same-id", payload={})
        for source in (InboundEventSource.SFTP, InboundEventSource.PUBSUB)
    ]
    db_session.add_all(events)
    db_session.flush()
    assert all(event.order_id is None for event in events)

    order = make_external_order()
    db_session.add(order)
    for event in events:
        event.order = order
        event.status = InboundEventStatus.PROCESSED
        event.processed_at = datetime.now(UTC)
    db_session.flush()
    db_session.expire_all()

    assert {event.order_id for event in order.inbound_events} == {order.id}
    assert len(order.inbound_events) == 2
    assert all(event.processed_at is not None for event in order.inbound_events)


def test_failed_event_retains_retry_information(db_session):
    now = datetime.now(UTC)
    event = InboundEvent(
        source=InboundEventSource.RECALL,
        external_event_id="recall-1",
        payload={},
        status=InboundEventStatus.FAILED,
        attempt_count=2,
        last_attempt_at=now,
        next_retry_at=now + timedelta(seconds=30),
        last_error="Upstream timeout",
    )
    db_session.add(event)
    db_session.flush()
    db_session.expire_all()

    saved = db_session.get(InboundEvent, event.id)
    assert saved.status is InboundEventStatus.FAILED
    assert saved.attempt_count == 2
    assert saved.last_error == "Upstream timeout"
    assert saved.next_retry_at - saved.last_attempt_at == timedelta(seconds=30)
    assert saved.processed_at is None


def test_messaging_event_retains_complete_decision_audit(db_session):
    responded_at = datetime.now(UTC)
    event = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id="message-1",
        payload={"text": "Necesito dos kits", "receivedAt": "2026-10-27T16:05:00Z"},
        status=InboundEventStatus.PROCESSED,
        llm_output={"is_order": True, "confidence": 0.96},
        interpreted_sku="KIT-RESP-ADULTO",
        interpreted_quantity=2,
        interpreted_delivery_date=datetime.fromisoformat("2026-10-27T22:00:00+00:00"),
        decision=MessagingDecision.ORDER,
        external_order_id="0123456789abcdef01234567",
        responded_at=responded_at,
    )
    db_session.add(event)
    db_session.flush()
    db_session.expire_all()

    saved = db_session.get(InboundEvent, event.id)
    assert saved.payload["text"] == "Necesito dos kits"
    assert saved.llm_output == {"is_order": True, "confidence": 0.96}
    assert saved.interpreted_sku == "KIT-RESP-ADULTO"
    assert saved.interpreted_quantity == 2
    assert saved.interpreted_delivery_date == datetime.fromisoformat(
        "2026-10-27T22:00:00+00:00"
    )
    assert saved.decision is MessagingDecision.ORDER
    assert saved.external_order_id == "0123456789abcdef01234567"
    assert saved.responded_at == responded_at


def test_discarded_message_retains_raw_llm_output_and_reason(db_session):
    event = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id="message-discarded",
        payload={"text": "¿Ya despacharon mi pedido?"},
        llm_output="The message is not a new order",
        decision=MessagingDecision.DISCARDED,
        reason="The message asks about an existing order",
        responded_at=datetime.now(UTC),
    )
    db_session.add(event)
    db_session.flush()
    db_session.expire_all()

    saved = db_session.get(InboundEvent, event.id)
    assert saved.payload == {"text": "¿Ya despacharon mi pedido?"}
    assert saved.llm_output == "The message is not a new order"
    assert saved.decision is MessagingDecision.DISCARDED
    assert saved.reason == "The message asks about an existing order"
    assert saved.order_id is None
    assert saved.external_order_id is None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"external_event_id": ""}, "ck_inbound_events_external_id_not_empty"),
        ({"external_event_id": "   "}, "ck_inbound_events_external_id_not_empty"),
        ({"attempt_count": -1}, "ck_inbound_events_attempt_count"),
        (
            {"interpreted_quantity": 0},
            "ck_inbound_events_interpreted_quantity",
        ),
        ({"payload": []}, "ck_inbound_events_payload_object"),
    ],
)
def test_invalid_deduplication_or_retry_data_is_rejected(
    db_session, overrides, constraint
):
    values = {
        "source": InboundEventSource.SFTP,
        "external_event_id": "orders.xml",
        "payload": {},
    }
    values.update(overrides)
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(InboundEvent(**values))
        db_session.flush()
    assert error.value.orig.diag.constraint_name == constraint


def test_event_cannot_reference_missing_order(db_session):
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            InboundEvent(
                source=InboundEventSource.MESSAGING,
                external_event_id="message-1",
                payload={},
                order_id=uuid4(),
            )
        )
        db_session.flush()


@pytest.mark.parametrize("column", ["external_event_id", "payload"])
def test_event_identity_and_payload_cannot_be_null(db_session, column):
    values = {
        "source": InboundEventSource.SFTP,
        "external_event_id": "orders.xml",
        "payload": {},
        column: None,
    }
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(InboundEvent(**values))
        db_session.flush()
    assert error.value.orig.diag.column_name == column
