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
from tests.support.oidc import SERVICE_ACCOUNT, TestTokenIssuer

AUDIENCE = "https://choripan3.ing.uc.cl/api/recalls"


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
            recall_oidc_audience=AUDIENCE,
            recall_oidc_service_account=SERVICE_ACCOUNT,
        ),
    )
    try:
        yield api_client, verifier
    finally:
        verifier.close()


def recall(**overrides):
    values = {
        "recallId": "rcl_6721a0b3",
        "batch": "L-EXCLACTOSA-261026-3B9C",
        "sku": "EXC-LACTOSA-DC",
        "reason": "Contaminación detectada por el proveedor",
        "issuedAt": "2026-10-28T14:12:00Z",
        "deadline": "2026-10-28T14:42:00Z",
    }
    values.update(overrides)
    return values


def post(client, issuer, payload=None, **token_overrides):
    return client.post(
        "/api/recalls",
        json=recall() if payload is None else payload,
        headers={
            "Authorization": (f"Bearer {issuer.token(aud=AUDIENCE, **token_overrides)}")
        },
    )


def test_persists_recall_before_ack_without_processing(
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
    assert saved.source is InboundEventSource.RECALL
    assert saved.external_event_id == recall()["recallId"]
    assert saved.payload == recall()
    assert saved.status is InboundEventStatus.PENDING
    assert saved.processed_at is None
    assert saved.attempt_count == 0


@pytest.mark.parametrize("already_processed", [False, True])
def test_duplicate_preserves_the_original_recall(
    authenticated_client, issuer, db_session, already_processed
):
    client, _ = authenticated_client
    assert post(client, issuer).status_code == 204
    saved = db_session.scalars(select(InboundEvent)).one()
    if already_processed:
        saved.status = InboundEventStatus.PROCESSED
        saved.processed_at = datetime.fromisoformat("2026-10-28T14:20:00+00:00")
        saved.attempt_count = 1
        db_session.commit()

    changed = recall(reason="Contenido distinto en un reintento")
    assert post(client, issuer, changed).status_code == 204

    db_session.refresh(saved)
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 1
    assert saved.payload == recall()
    assert saved.status is (
        InboundEventStatus.PROCESSED
        if already_processed
        else InboundEventStatus.PENDING
    )


@pytest.mark.parametrize(
    "authorization", [None, "", "Basic token", "Bearer", "Bearer invalid"]
)
def test_invalid_authentication_never_persists(
    authenticated_client, db_session, authorization
):
    client, _ = authenticated_client
    headers = {} if authorization is None else {"Authorization": authorization}

    response = client.post("/api/recalls", json=recall(), headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0


def test_wrong_audience_or_sender_returns_401(authenticated_client, issuer, db_session):
    client, _ = authenticated_client
    wrong_audience = client.post(
        "/api/recalls",
        json=recall(),
        headers={"Authorization": f"Bearer {issuer.token()}"},
    )
    wrong_sender = post(client, issuer, email="other@example.test")

    assert wrong_audience.status_code == 401
    assert wrong_sender.status_code == 401
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"recallId": ""},
        {"batch": "  "},
        {"sku": None},
        {"reason": ""},
        {"issuedAt": "2026-10-28T14:12:00"},
        {"deadline": "invalid"},
        {"deadline": "2026-10-28T14:12:00Z"},
        {"deadline": "2026-10-28T14:11:59Z"},
    ],
)
def test_invalid_payload_returns_422_without_persisting(
    authenticated_client, issuer, db_session, overrides
):
    client, _ = authenticated_client

    response = post(client, issuer, recall(**overrides))

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

    def fail():
        raise OperationalError("commit", None, Exception("Database unavailable"))

    with monkeypatch.context() as context:
        context.setattr(db_session, "commit", fail)
        assert post(client, issuer).status_code == 503

    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 0
    assert post(client, issuer).status_code == 204
    assert db_session.scalar(select(func.count()).select_from(InboundEvent)) == 1
