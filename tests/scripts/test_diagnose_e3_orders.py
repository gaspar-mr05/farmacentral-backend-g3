import json
import stat
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models import (
    InboundEvent,
    InboundEventSource,
    MessagingDecision,
    ProductionRun,
    ReplenishmentState,
)
from scripts import diagnose_e3_orders as module
from tests.support.billing import make_period
from tests.support.farma_central import make_settings
from tests.support.orders import make_external_order


def test_report_correlates_orders_and_state_without_customer_data(db_session):
    now = datetime.now(UTC)
    period = make_period(starts_at=now - timedelta(hours=2))
    db_session.add(period)
    db_session.flush()
    db_session.add_all(
        [
            make_external_order(
                buyer_name="PRIVATE_CUSTOMER", created_at=now - timedelta(minutes=10)
            ),
            make_external_order(
                external_order_id="e" * 24, created_at=now - timedelta(minutes=5)
            ),
            ProductionRun(
                expected_sku="KIT-ANALGESIA",
                expected_quantity=2,
                requested_at=now - timedelta(minutes=20),
                available_at=now - timedelta(minutes=5),
            ),
            ReplenishmentState(
                period_id=period.id,
                sku="KIT-ANALGESIA",
                goal={
                    "quantity": 2,
                    "order_id": "selected-order",
                    "private": "PRIVATE_GOAL",
                },
                pending_operation={
                    "status": "uncertain",
                    "private": "PRIVATE_OPERATION",
                },
            ),
            InboundEvent(
                source=InboundEventSource.MESSAGING,
                external_event_id="msg-real",
                received_at=now,
                payload={"text": "PRIVATE_TEXT", "sender": {"phone": "PRIVATE_PHONE"}},
                decision=MessagingDecision.INQUIRY,
                last_error="Messaging returned HTTP 404 PRIVATE_ERROR",
                resolution_evidence={
                    "delivery": {
                        "reply": {
                            "state": "uncertain",
                            "to": "PRIVATE_PHONE",
                            "text": "PRIVATE_REPLY",
                        }
                    }
                },
            ),
            InboundEvent(
                source=InboundEventSource.MESSAGING,
                external_event_id="old-message",
                received_at=now - timedelta(days=1),
                payload={},
            ),
        ]
    )
    db_session.flush()
    report = module.build_report(
        db_session,
        make_settings(app_env="dev", billing_period_id=str(period.id)),
        limit=1,
    )
    serialized = json.dumps(report, default=str)
    assert "PRIVATE_" not in serialized
    assert report["truncated"]["orders"]
    assert sum(row["count"] for row in report["order_counts"]) == 2
    assert report["orders"][0]["items"][0]["stock_for_deadline"] == 0
    assert report["orders"][0]["items"][0]["confirmed_production_for_deadline"] == 0
    assert report["production"][0]["past_availability_without_completion"]
    assert report["messages"][0]["error_flags"]["http_404"]
    assert report["messages"][0]["delivery"]["reply"] == {"state": "uncertain"}
    assert "old-message" not in serialized
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_postgres_actually_rejects_writes_inside_diagnostic_snapshot(
    test_engine, monkeypatch
):
    def inspect(session, settings, **kwargs):
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        with pytest.raises(DBAPIError) as error, session.begin_nested():
            session.execute(
                text("CREATE TABLE diagnostic_write_must_fail (id integer)")
            )
        assert error.value.orig.sqlstate == "25006"
        return {"read_only": True}

    monkeypatch.setattr(module, "build_report", inspect)
    assert module.read_report(test_engine, make_settings()) == {"read_only": True}


def test_file_is_private_and_not_overwritten(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    calls = []

    def report(*args, **kwargs):
        calls.append(True)
        return {"truncated": {}, "private_evidence": "REPORT_CONTENT"}

    monkeypatch.setattr(module, "read_report", report)
    path = tmp_path / "diagnostic.json"
    assert module.main(["--output", str(path)]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    original = path.read_bytes()
    assert module.main(["--output", str(path)]) == 1
    assert path.read_bytes() == original
    assert calls == [True]
    assert "REPORT_CONTENT" not in capsys.readouterr().out


def test_configuration_failure_does_not_expose_credentials(
    tmp_path, monkeypatch, capsys
):
    def invalid():
        raise ValueError("PRIVATE_CREDENTIALS")

    monkeypatch.setattr(module, "get_settings", invalid)
    assert module.main(["--output", str(tmp_path / "report.json")]) == 1
    assert "PRIVATE_CREDENTIALS" not in capsys.readouterr().out
