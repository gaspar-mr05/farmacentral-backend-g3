from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import Base, InboundEvent, InboundEventSource, InboundEventStatus
from app.schemas.recalls import RecallWebhookPayload
from app.services.recalls import RecallWebhookService


@pytest.fixture
def recall_database(test_engine):
    schema = f"recall_{uuid4().hex}"
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


def recall():
    return RecallWebhookPayload.model_validate(
        {
            "recallId": "rcl_durable",
            "batch": "L-EXCLACTOSA-261026-3B9C",
            "sku": "EXC-LACTOSA-DC",
            "reason": "Contaminación detectada",
            "issuedAt": "2026-10-28T14:12:00Z",
            "deadline": "2026-10-28T14:42:00Z",
        }
    )


def test_recall_survives_session_restart(recall_database):
    with Session(recall_database) as session:
        RecallWebhookService(session).receive(recall())

    with Session(recall_database) as session:
        saved = session.scalars(select(InboundEvent)).one()
        assert saved.source is InboundEventSource.RECALL
        assert saved.payload == recall().model_dump(mode="json", by_alias=True)
        assert saved.status is InboundEventStatus.PENDING


def test_concurrent_webhooks_persist_once(recall_database):
    ready = Barrier(2)

    def receive():
        with Session(recall_database) as session:
            ready.wait(timeout=5)
            RecallWebhookService(session).receive(recall())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(receive)
        second = executor.submit(receive)
        first.result(timeout=10)
        second.result(timeout=10)

    with Session(recall_database) as session:
        saved = session.scalars(select(InboundEvent)).one()
        assert saved.external_event_id == recall().recall_id
        assert saved.payload == recall().model_dump(mode="json", by_alias=True)
