from datetime import datetime
from time import perf_counter

import pytest
from google.auth.exceptions import TransportError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.api import dependencies
from app.api.dependencies import get_oidc_verifier
from app.main import app
from app.models import InboundEvent, InboundEventSource, InboundEventStatus
from tests.support.farma_central import make_settings
from tests.support.oidc import AUDIENCE, TestTokenIssuer


@pytest.fixture(scope="module")
def issuer():
    return TestTokenIssuer()


@pytest.fixture
def authenticated_client(api_client, issuer, monkeypatch):
    verifier = issuer.verifier()
    app.dependency_overrides[get_oidc_verifier] = lambda: verifier
    monkeypatch.setattr(
        dependencies,
        "get_settings",
        lambda: make_settings(
            messaging_oidc_audience=AUDIENCE,
        ),
    )
    try:
        yield api_client, verifier
    finally:
        verifier.close()


def message(**overrides):
    values = {
        "messageId": "msg_01JB7Q2F4R8S",
        "sender": {"name": "Farmacia Los Aromos", "phone": "+56 9 5555 0123"},
        "text": "Necesito dos kits respiratorios para hoy antes de las 7.",
        "receivedAt": "2026-10-27T16:05:00Z",
    }
    values.update(overrides)
    return values


def post(client, issuer, payload=None):
    return client.post(
        "/api/messaging",
        json=message() if payload is None else payload,
        headers={"Authorization": f"Bearer {issuer.token()}"},
    )


def test_persists_message_before_ack_without_processing(
    authenticated_client, issuer, db_session
):
    client, _ = authenticated_client
    started = perf_counter()

    response = post(client, issuer)

    assert response.status_code == 204
    assert response.content == b""
    assert perf_counter() - started < 15
    db_session.expire_all()
    saved = db_session.scalars(select(InboundEvent)).one()
    assert saved.source == InboundEventSource.MESSAGING
    assert saved.external_event_id == message()["messageId"]
    assert saved.payload == message()
    assert saved.status == InboundEventStatus.PENDING
    assert saved.order_id is None
    assert saved.processed_at is None
    assert saved.attempt_count == 0
    assert saved.received_at is not None


@pytest.mark.parametrize("already_processed", [False, True])
def test_duplicate_does_not_overwrite_or_reprocess_original(
    authenticated_client, issuer, db_session, already_processed
):
    client, _ = authenticated_client
    assert post(client, issuer).status_code == 204
    saved = db_session.scalars(select(InboundEvent)).one()
    if already_processed:
        saved.status = InboundEventStatus.PROCESSED
        saved.processed_at = datetime.fromisoformat("2026-10-27T16:06:00+00:00")
        saved.attempt_count = 1
        db_session.commit()
    changed = message(text="Contenido distinto en un reintento")

    assert post(client, issuer, changed).status_code == 204

    db_session.refresh(saved)
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 1
    assert saved.payload == message()
    assert saved.status == (
        InboundEventStatus.PROCESSED
        if already_processed
        else InboundEventStatus.PENDING
    )
    assert saved.attempt_count == int(already_processed)


@pytest.mark.parametrize(
    "authorization", [None, "", "Basic token", "Bearer", "Bearer invalid"]
)
def test_invalid_authentication_never_persists(
    authenticated_client, db_session, authorization
):
    client, _ = authenticated_client
    headers = {} if authorization is None else {"Authorization": authorization}

    response = client.post("/api/messaging", json=message(), headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0


def test_wrong_audience_returns_401_even_with_matching_host_header(
    authenticated_client, issuer, db_session
):
    client, _ = authenticated_client
    response = client.post(
        "/api/messaging",
        json=message(),
        headers={
            "Authorization": f"Bearer {issuer.token(aud='https://attacker.test/api/messaging')}",
            "Host": "attacker.test",
            "X-Forwarded-Host": "attacker.test",
        },
    )
    assert response.status_code == 401
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"messageId": ""},
        {"messageId": "  "},
        {"messageId": None},
        {"sender": {}},
        {"sender": {"name": "Farmacia", "phone": ""}},
        {"text": ""},
        {"text": None},
        {"receivedAt": "2026-10-27T16:05:00"},
        {"receivedAt": "invalid"},
    ],
)
def test_invalid_payload_returns_422_without_persisting(
    authenticated_client, issuer, db_session, overrides
):
    client, _ = authenticated_client
    response = post(client, issuer, message(**overrides))
    assert response.status_code == 422
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0


def test_certificate_outage_returns_503_without_ack(
    authenticated_client, issuer, db_session
):
    client, verifier = authenticated_client

    def fail(url, **kwargs):
        raise TransportError("Signing certificate fetch timed out")

    verifier._request = fail
    assert post(client, issuer).status_code == 503
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0


def test_database_failure_returns_503_then_redelivery_can_succeed(
    authenticated_client, issuer, db_session, monkeypatch
):
    client, _ = authenticated_client
    original_commit = db_session.commit

    def fail():
        raise OperationalError("commit", None, Exception("Database unavailable"))

    with monkeypatch.context() as context:
        context.setattr(db_session, "commit", fail)
        assert post(client, issuer).status_code == 503

    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0
    assert db_session.commit == original_commit
    assert post(client, issuer).status_code == 204
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 1
