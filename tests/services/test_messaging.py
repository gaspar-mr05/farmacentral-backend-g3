from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import Base, InboundEvent, InboundEventSource, InboundEventStatus
from app.schemas.messaging import MessagingWebhookPayload
from app.services.messaging import MessagingWebhookService


@pytest.fixture
def messaging_database(test_engine):
    schema = f"messaging_{uuid4().hex}"
    with test_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(
        test_engine.url,
        connect_args={"options": f"-csearch_path={schema} -ctimezone=UTC"},
    )
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with test_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


def message():
    return MessagingWebhookPayload.model_validate(
        {
            "messageId": "msg_durable",
            "sender": {"name": "Farmacia", "phone": "+56 9 5555 0123"},
            "text": "Necesito dos kits.",
            "receivedAt": "2026-10-27T16:05:00Z",
        }
    )


def test_message_survives_session_restart(messaging_database):
    with Session(messaging_database) as session:
        MessagingWebhookService(session).receive(message())

    with Session(messaging_database) as session:
        saved = session.scalars(select(InboundEvent)).one()
        assert saved.source == InboundEventSource.MESSAGING
        assert saved.payload == message().model_dump(mode="json", by_alias=True)
        assert saved.status == InboundEventStatus.PENDING
        MessagingWebhookService(session).receive(message())

    with Session(messaging_database) as session:
        assert len(session.scalars(select(InboundEvent)).all()) == 1


def test_concurrent_webhooks_persist_once(messaging_database):
    ready = Barrier(2)

    def receive():
        with Session(messaging_database) as session:
            ready.wait(timeout=5)
            MessagingWebhookService(session).receive(message())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(receive)
        second = executor.submit(receive)
        first.result(timeout=10)
        second.result(timeout=10)

    with Session(messaging_database) as session:
        saved = session.scalars(select(InboundEvent)).one()
        assert saved.external_event_id == message().message_id
        assert saved.payload == message().model_dump(mode="json", by_alias=True)
