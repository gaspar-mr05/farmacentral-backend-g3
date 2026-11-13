from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select

from app.api import dependencies
from app.api.routes import operations
from app.models import BusinessMetricSnapshot, MarketOffer, WorkerHeartbeat
from app.services.billing.metrics import BusinessMetricsService
from tests.support.billing import make_invoice, make_period
from tests.support.farma_central import make_settings
from tests.support.orders import make_external_order

KEY = "private-operations-test-key"
HEADERS = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def settings(monkeypatch):
    config = make_settings(
        farma_central_base_url="https://dev.proyecto.2026-2.tallerdeintegracion.cl/farma-central/",
        operations_api_key=KEY,
    )
    monkeypatch.setattr(dependencies, "get_settings", lambda: config)
    monkeypatch.setattr(operations, "get_settings", lambda: config)
    return config


@pytest.fixture
def period(db_session, settings):
    row = make_period(starts_at=datetime.now(UTC) - timedelta(hours=1))
    db_session.add(row)
    db_session.commit()
    return row


@pytest.mark.parametrize(
    "path", ["waiting-orders", "offers", "invoices", "metrics", "workers"]
)
@pytest.mark.parametrize("auth", [None, "Bearer wrong-key", "Basic dXNlcjpwYXNz"])
def test_diagnostics_require_authentication(api_client, settings, path, auth):
    response = api_client.get(
        f"/api/operations/{path}?period_id={uuid4()}",
        headers={"Authorization": auth} if auth else {},
    )
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert KEY not in response.text


def test_operations_are_disabled_without_a_separate_key(api_client, settings):
    settings.operations_api_key = None
    response = api_client.get(
        f"/api/operations/invoices?period_id={uuid4()}", headers=HEADERS
    )
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("scope", ["unknown", "other_group", "other_environment"])
def test_period_scope_cannot_be_bypassed(api_client, db_session, settings, scope):
    row = make_period(
        group_id=4 if scope == "other_group" else 3,
        environment="prod" if scope == "other_environment" else "dev",
    )
    db_session.add(row)
    db_session.commit()
    selected_id = uuid4() if scope == "unknown" else row.id
    response = api_client.get(
        f"/api/operations/invoices?period_id={selected_id}",
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Operations period not found"}


def test_waiting_orders_are_bounded_sorted_and_do_not_expose_customer_data(
    api_client, db_session, period
):
    now = datetime.now(UTC)
    for index, minutes in enumerate((30, 10, 20)):
        db_session.add(
            make_external_order(
                external_order_id=f"{index + 1:024x}",
                delivery_deadline=now + timedelta(minutes=minutes),
                decision_reason="Waiting for stock",
            )
        )
    db_session.add(
        make_external_order(
            external_order_id="f" * 24, created_at=period.starts_at - timedelta(hours=1)
        )
    )
    db_session.commit()
    base = f"/api/operations/waiting-orders?period_id={period.id}&limit=2"
    first = api_client.get(base, headers=HEADERS)
    assert first.status_code == 200
    body = first.json()
    assert body["has_more"] and body["data_source"] == "persisted"
    assert [item["external_order_id"] for item in body["items"]] == [
        f"{2:024x}",
        f"{3:024x}",
    ]
    assert "buyer_email" not in body["items"][0]
    second = api_client.get(base + "&offset=2", headers=HEADERS).json()
    assert not second["has_more"]
    assert [item["external_order_id"] for item in second["items"]] == [f"{1:024x}"]


def test_read_only_diagnostics_work_when_all_remote_services_are_down(
    api_client, db_session, period, monkeypatch
):
    blocked = AsyncMock(
        side_effect=AssertionError("Diagnostics must not call remote services")
    )
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    offer = MarketOffer(
        period_id=period.id,
        external_offer_id="a" * 24,
        side="sell",
        publisher_group_id=3,
        sku="RAW",
        quantity=2,
        unit_price="1.23456789",
        status="creation_uncertain",
        last_error="private error body that must not be exposed",
        http_status=503,
    )
    db_session.add_all([offer, make_invoice(period.id)])
    db_session.commit()
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lstrip().split()[0].upper())

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        for path in ("waiting-orders", "offers", "invoices", "metrics", "workers"):
            response = api_client.get(
                f"/api/operations/{path}?period_id={period.id}", headers=HEADERS
            )
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json()["data_source"] == "persisted"
            assert "private error body" not in response.text
        invoices = api_client.get(
            f"/api/operations/invoices?period_id={period.id}", headers=HEADERS
        ).json()
        assert invoices["items"][0]["principal"] == "100.123456789"
        offers = api_client.get(
            f"/api/operations/offers?period_id={period.id}", headers=HEADERS
        ).json()
        assert offers["items"][0]["has_error"]
        assert offers["items"][0]["last_checked_at"] is None
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    blocked.assert_not_awaited()
    assert set(statements) == {"SELECT"}
    assert not db_session.dirty and not db_session.new
    assert db_session.scalar(select(MarketOffer.status)) == "creation_uncertain"


def test_metrics_report_capture_time_without_recalculating(
    api_client, db_session, period
):
    base = f"/api/operations/metrics?period_id={period.id}"
    empty = api_client.get(base, headers=HEADERS).json()
    assert empty["state"] == "not_captured" and empty["metrics"] is None
    captured = datetime.now(UTC) - timedelta(minutes=20)
    report = BusinessMetricsService(db_session, period).calculate(now=captured)
    snapshot = BusinessMetricSnapshot(
        period_id=period.id,
        captured_at=captured,
        commit_sha="a" * 40,
        metrics=report.model_dump(mode="json"),
    )
    db_session.add(snapshot)
    db_session.commit()
    body = api_client.get(base, headers=HEADERS).json()
    assert body["state"] == "captured"
    assert (
        body["metrics"]["calculated_at"]
        == report.model_dump(mode="json")["calculated_at"]
    )
    assert body["metrics"]["reconciliation"]["full_scan_pending"]
    assert db_session.scalars(select(BusinessMetricSnapshot)).all() == [snapshot]


def test_financial_data_is_not_mixed_between_periods(api_client, db_session, period):
    other = make_period(code="other-period")
    db_session.add(other)
    db_session.flush()
    db_session.add_all(
        [
            make_invoice(period.id),
            make_invoice(other.id, counterparty_id="private-other-period"),
            BusinessMetricSnapshot(
                period_id=other.id,
                commit_sha="b" * 40,
                metrics=BusinessMetricsService(db_session, other)
                .calculate()
                .model_dump(mode="json"),
            ),
        ]
    )
    db_session.commit()
    body = api_client.get(
        f"/api/operations/invoices?period_id={period.id}",
        headers=HEADERS,
    ).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["counterparty_id"] == "1000"
    response = api_client.get(
        f"/api/operations/metrics?period_id={period.id}",
        headers=HEADERS,
    )
    assert response.json()["state"] == "not_captured"


def test_worker_diagnostics_expose_progress_and_queue_age(
    api_client, db_session, period
):
    now = datetime.now(UTC)
    db_session.add(
        WorkerHeartbeat(
            period_id=period.id,
            worker_name="process_billing",
            heartbeat_at=now - timedelta(seconds=5),
            last_started_at=now - timedelta(seconds=10),
            last_success_at=now - timedelta(minutes=1),
            last_failure_at=None,
            last_error_type=None,
            consecutive_failures=0,
            queue_depth=3,
            oldest_queued_at=now - timedelta(minutes=5),
        )
    )
    db_session.commit()
    response = api_client.get(
        f"/api/operations/workers?period_id={period.id}", headers=HEADERS
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["worker_name"] == "process_billing"
    assert item["queue_depth"] == 3
    assert item["queue_age_seconds"] >= 299
    assert item["last_success_at"] is not None


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_page_limits_are_validated(api_client, period, query):
    assert (
        api_client.get(
            f"/api/operations/offers?period_id={period.id}&{query}", headers=HEADERS
        ).status_code
        == 422
    )


def test_private_routes_do_not_offer_commands(api_client, period):
    for path in ("waiting-orders", "offers", "invoices", "metrics", "workers"):
        assert (
            api_client.post(
                f"/api/operations/{path}?period_id={period.id}", headers=HEADERS
            ).status_code
            == 405
        )
