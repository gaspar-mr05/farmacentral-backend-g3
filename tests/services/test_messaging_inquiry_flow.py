from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.llm_gateway import LLMGatewayRateLimitError
from app.clients.messaging import (
    MessagingAlreadyRespondedError,
    MessagingConnectionError,
    MessagingHTTPError,
    MessagingInvalidResponseError,
    MessagingTimeoutError,
)
from app.models import (
    InboundEvent,
    InboundEventStatus,
    MessageDirection,
    MessagingDecision,
    MessagingMessage,
)
from app.services.messaging import MessageInterpretationService
from app.services.messaging import inquiry_flow as flow_module
from app.services.messaging import processing as worker_module
from tests.services.test_messaging import messaging_database as messaging_database
from tests.services.test_messaging_processing import (
    NOW,
    FakeMessaging,
    FakeOrders,
    add_event,
    make_service,
)
from tests.support.orders import EXTERNAL_ORDER_ID


def inquiry(session):
    prior = add_event(
        session,
        decision=MessagingDecision.ORDER,
        received_at=NOW - timedelta(minutes=10),
        message_id="prior-order",
    )
    prior.external_order_id = EXTERNAL_ORDER_ID
    prior.status = InboundEventStatus.PROCESSED
    prior.processed_at = NOW
    session.commit()
    event = add_event(session, decision=MessagingDecision.INQUIRY, message_id="query")
    event.payload = {**event.payload, "text": f"¿Cómo va la OC {EXTERNAL_ORDER_ID}?"}
    session.commit()
    return event


def retry(session, event):
    event.status = InboundEventStatus.PENDING
    event.next_retry_at = None
    session.commit()


@pytest.mark.anyio
async def test_restart_after_rate_limited_text_preserves_snapshot_and_classification(
    messaging_database,
):
    remote, orders = FakeMessaging(), FakeOrders()
    remote.reply_error = MessagingHTTPError(429, retry_after_seconds=20)
    with Session(messaging_database) as session:
        event = inquiry(session)
        event_id = event.id
        worker, _, interpreter, _ = make_service(
            session, messaging=remote, orders=orders
        )
        result = await worker.process_pending()
        assert (result.processed, result.failed) == (0, 1)
        delivery = event.resolution_evidence["delivery"]
        assert delivery["classification"]["state"] == "confirmed"
        assert delivery["reply"]["state"] == "pending"
        assert event.responded_at is None
        snapshot = event.order_status_snapshot
        text = delivery["reply"]["text"]
        assert interpreter.calls == 0
        retry(session, event)

    remote.reply_error = None
    with Session(messaging_database) as restarted:
        worker, _, interpreter, _ = make_service(
            restarted, messaging=remote, orders=orders
        )
        assert (await worker.process_pending()).processed == 1
        event = restarted.get(InboundEvent, event_id)
        delivery = event.resolution_evidence["delivery"]
        assert event.order_status_snapshot == snapshot
        assert delivery["reply"]["text"] == text
        assert delivery["classification"]["attempts"] == 1
        assert delivery["reply"]["attempts"] == 2
        assert delivery["reply"]["latencySeconds"] >= 0
        assert event.responded_at is not None
        assert interpreter.calls == 0
        rows = restarted.scalars(
            select(MessagingMessage).where(
                MessagingMessage.inbound_event_id == event.id,
                MessagingMessage.direction == MessageDirection.OUTBOUND,
            )
        ).all()
        assert len(rows) == 1 and rows[0].text == text
    assert orders.requested == [EXTERNAL_ORDER_ID]
    assert remote.inquiries == [("query", EXTERNAL_ORDER_ID)]
    assert len(remote.replies) == 2
    assert remote.replies[0] == remote.replies[1]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        MessagingTimeoutError("lost"),
        MessagingConnectionError("lost"),
        MessagingInvalidResponseError("unreadable acknowledgement"),
        MessagingHTTPError(500),
        MessagingHTTPError(409),
    ],
)
async def test_uncertain_text_is_not_repeated_even_if_manually_requeued(
    messaging_database,
    failure,
):
    remote = FakeMessaging()
    remote.reply_error = failure
    with Session(messaging_database) as session:
        event = inquiry(session)
        event_id = event.id
        worker, _, _, _ = make_service(session, messaging=remote)
        assert (await worker.process_pending()).failed == 1
        assert event.resolution_evidence["delivery"]["reply"]["state"] == "uncertain"
        assert event.responded_at is None
        assert event.next_retry_at is None
        assert "uncertain" in event.last_error
        retry(session, event)
    remote.reply_error = None
    with Session(messaging_database) as restarted:
        worker, _, interpreter, _ = make_service(restarted, messaging=remote)
        assert (await worker.process_pending()).failed == 1
        assert interpreter.calls == 0
        event = restarted.get(InboundEvent, event_id)
        assert event.processed_at is None
        assert (
            restarted.scalars(
                select(MessagingMessage).where(
                    MessagingMessage.direction == MessageDirection.OUTBOUND
                )
            ).all()
            == []
        )
    assert len(remote.inquiries) == len(remote.replies) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        MessagingTimeoutError("lost"),
        MessagingAlreadyRespondedError(409),
    ],
)
async def test_uncertain_classification_never_claims_text_was_sent(db_session, failure):
    remote = FakeMessaging()
    remote.inquiry_error = failure
    event = inquiry(db_session)
    worker, _, _, _ = make_service(db_session, messaging=remote)
    assert (await worker.process_pending()).failed == 1
    delivery = event.resolution_evidence["delivery"]
    assert delivery["classification"]["state"] == "uncertain"
    assert delivery["reply"]["state"] == "pending"
    assert remote.replies == []
    assert event.responded_at is None and event.next_retry_at is None


class Crash(BaseException):
    pass


@pytest.mark.anyio
async def test_crash_between_confirmations_resumes_only_the_pending_text(
    messaging_database,
):
    remote, orders = FakeMessaging(), FakeOrders()
    with Session(messaging_database) as session:
        event = inquiry(session)
        event_id = event.id
        worker, _, _, _ = make_service(session, messaging=remote, orders=orders)
        save = worker._inquiry_flow._save

        def crash_after_classification(event, delivery):
            save(event, delivery)
            if (
                delivery["classification"]["state"] == "confirmed"
                and delivery["reply"]["state"] == "pending"
            ):
                raise Crash()

        worker._inquiry_flow._save = crash_after_classification
        with pytest.raises(Crash):
            await worker.process_pending()
    with Session(messaging_database) as restarted:
        event = restarted.get(InboundEvent, event_id)
        event.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
        restarted.commit()
        worker, _, interpreter, _ = make_service(
            restarted, messaging=remote, orders=orders
        )
        assert (await worker.process_pending()).processed == 1
        assert interpreter.calls == 0
    assert len(remote.inquiries) == len(remote.replies) == 1
    assert orders.requested == [EXTERNAL_ORDER_ID]


@pytest.mark.anyio
async def test_crash_during_text_leaves_an_uncertain_result_without_blind_resend(
    messaging_database,
):
    class AppliedThenCrashed(FakeMessaging):
        async def send_reply(self, message, *, text):
            await super().send_reply(message, text=text)
            raise Crash()

    remote = AppliedThenCrashed()
    with Session(messaging_database) as session:
        event = inquiry(session)
        event_id = event.id
        worker, _, _, _ = make_service(session, messaging=remote)
        with pytest.raises(Crash):
            await worker.process_pending()
    with Session(messaging_database) as restarted:
        event = restarted.get(InboundEvent, event_id)
        event.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
        restarted.commit()
        worker, _, _, _ = make_service(restarted, messaging=remote)
        assert (await worker.process_pending()).failed == 1
        assert event.resolution_evidence["delivery"]["reply"]["state"] == "uncertain"
        assert event.responded_at is None
    assert len(remote.replies) == 1


@pytest.mark.anyio
async def test_deadline_between_classification_and_text_prevents_late_send(
    db_session,
    monkeypatch,
):
    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current

    class SlowClassification(FakeMessaging):
        async def respond_inquiry(self, *args, **kwargs):
            result = await super().respond_inquiry(*args, **kwargs)
            Clock.current = NOW + timedelta(minutes=31)
            return result

    monkeypatch.setattr(flow_module, "datetime", Clock)
    remote = SlowClassification()
    event = inquiry(db_session)
    worker, _, _, _ = make_service(db_session, messaging=remote)
    assert (await worker.process_pending()).failed == 1
    assert remote.replies == []
    assert event.responded_at is None
    assert "expired" in event.last_error


@pytest.mark.anyio
async def test_earliest_original_message_is_processed_before_a_later_arrival(
    db_session,
):
    first = add_event(
        db_session,
        decision=MessagingDecision.DISCARDED,
        received_at=NOW - timedelta(minutes=1),
        message_id="newer-message",
    )
    second = add_event(
        db_session,
        decision=MessagingDecision.DISCARDED,
        received_at=NOW - timedelta(minutes=20),
        message_id="earlier-message",
    )
    assert first.received_at <= second.received_at
    worker, remote, _, _ = make_service(db_session)
    worker._settings.messaging_batch_size = 1
    assert (await worker.process_pending()).processed == 1
    assert remote.discards[0][0] == "earlier-message"


@pytest.mark.anyio
async def test_crash_after_reply_ack_can_finish_local_state_after_the_deadline(
    messaging_database,
    monkeypatch,
):
    remote = FakeMessaging()
    with Session(messaging_database) as session:
        event = inquiry(session)
        event_id = event.id
        worker, _, _, _ = make_service(session, messaging=remote)
        save = worker._inquiry_flow._save

        def crash_after_reply(event, delivery):
            save(event, delivery)
            if delivery["reply"]["state"] == "confirmed":
                raise Crash()

        worker._inquiry_flow._save = crash_after_reply
        with pytest.raises(Crash):
            await worker.process_pending()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(minutes=31)

    monkeypatch.setattr(worker_module, "datetime", Clock)
    monkeypatch.setattr(flow_module, "datetime", Clock)
    with Session(messaging_database) as restarted:
        worker, _, interpreter, _ = make_service(restarted, messaging=remote)
        assert (await worker.process_pending()).processed == 1
        event = restarted.get(InboundEvent, event_id)
        assert event.status is InboundEventStatus.PROCESSED
        assert event.responded_at < NOW + timedelta(minutes=30)
        assert interpreter.calls == 0
    assert len(remote.inquiries) == len(remote.replies) == 1


@pytest.mark.anyio
async def test_llm_cooldown_does_not_block_an_already_interpreted_inquiry(db_session):
    event = inquiry(db_session)
    add_event(
        db_session, received_at=NOW - timedelta(minutes=5), message_id="needs-llm"
    )
    interpreter = AsyncMock(spec=MessageInterpretationService)
    interpreter.interpret.side_effect = LLMGatewayRateLimitError(60)
    worker, remote, _, _ = make_service(db_session, interpreter=interpreter)
    result = await worker.process_pending()
    assert (result.processed, result.failed) == (1, 1)
    assert result.retry_after_seconds == 0
    assert event.status is InboundEventStatus.PROCESSED
    assert len(remote.inquiries) == len(remote.replies) == 1
    interpreter.interpret.assert_awaited_once()


@pytest.mark.anyio
async def test_saved_reply_cannot_be_rerouted_after_classification(db_session):
    remote = FakeMessaging()
    remote.reply_error = MessagingHTTPError(429)
    event = inquiry(db_session)
    worker, _, _, _ = make_service(db_session, messaging=remote)
    assert (await worker.process_pending()).failed == 1
    event.payload = {
        **event.payload,
        "sender": {"name": "Other", "phone": "+56999999999"},
    }
    retry(db_session, event)
    remote.reply_error = None
    assert (await worker.process_pending()).failed == 1
    assert "original query" in event.last_error
    assert len(remote.inquiries) == len(remote.replies) == 1
