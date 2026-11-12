from datetime import UTC, datetime, timedelta

from app.db.messaging_messages import (
    add_inbound_message,
    add_outbound_message,
    conversation_history,
    normalize_phone,
)
from app.models import (
    BusinessPeriod,
    InboundEvent,
    InboundEventSource,
    MessageDirection,
)
from app.schemas.messaging import MessagingWebhookPayload
from tests.support.farma_central import make_settings

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def incoming(message_id, phone, name="Farmacia homónima"):
    return MessagingWebhookPayload.model_validate(
        {
            "messageId": message_id,
            "sender": {"name": name, "phone": phone},
            "text": "Necesito un kit",
            "receivedAt": NOW.isoformat(),
        }
    )


def event(message_id):
    return InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id=message_id,
        payload={"messageId": message_id},
    )


def test_normalizes_common_phone_formats():
    assert normalize_phone("+56 9 5555 0123") == "+56955550123"
    assert normalize_phone("00 56 9 5555 0123") == "+56955550123"
    assert normalize_phone("9 5555 0123") == "+56955550123"


def test_history_is_ordered_and_isolated_by_phone_group_environment_and_period(
    db_session,
):
    period = BusinessPeriod(
        environment="dev",
        code="E3",
        group_id=3,
        starts_at=NOW - timedelta(days=1),
        ends_at=NOW + timedelta(days=1),
    )
    other_period = BusinessPeriod(
        environment="dev",
        code="E4",
        group_id=3,
        starts_at=NOW + timedelta(days=2),
    )
    db_session.add_all([period, other_period])
    db_session.flush()
    settings = make_settings(
        farma_central_base_url="https://dev.example.test/", app_env="dev"
    )

    first_event = event("first")
    second_event = event("second")
    homonym_event = event("homonym")
    db_session.add_all([first_event, second_event, homonym_event])
    db_session.flush()
    add_inbound_message(
        db_session, first_event, incoming("first", "+56 9 5555 0123"), settings
    )
    db_session.flush()
    add_outbound_message(
        db_session,
        first_event,
        payload={"status": "discarded"},
        text="Respuesta",
        occurred_at=NOW + timedelta(seconds=1),
    )
    add_inbound_message(
        db_session, second_event, incoming("second", "955550123"), settings
    )
    add_inbound_message(
        db_session, homonym_event, incoming("homonym", "+56 9 4444 0123"), settings
    )
    db_session.flush()

    history = conversation_history(
        db_session,
        phone="955550123",
        group_id=3,
        environment="dev",
        period_id=period.id,
    )

    assert [row.direction for row in history] == [
        MessageDirection.INBOUND,
        MessageDirection.INBOUND,
        MessageDirection.OUTBOUND,
    ]
    assert {row.external_message_id for row in history if row.external_message_id} == {
        "first",
        "second",
    }
    assert all(row.period_id == period.id for row in history)
    assert (
        conversation_history(
            db_session,
            phone="+56944440123",
            group_id=3,
            environment="dev",
            period_id=period.id,
        )[0].external_message_id
        == "homonym"
    )
    assert (
        conversation_history(
            db_session,
            phone="955550123",
            group_id=4,
            environment="dev",
            period_id=period.id,
        )
        == []
    )
    assert (
        conversation_history(
            db_session,
            phone="955550123",
            group_id=3,
            environment="dev",
            period_id=other_period.id,
        )
        == []
    )
