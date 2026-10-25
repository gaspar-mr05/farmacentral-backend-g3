from unittest.mock import Mock

from sqlalchemy.exc import OperationalError

from app.db import health


def test_readiness_uses_a_separate_read_only_bounded_connection(monkeypatch):
    create = Mock()
    monkeypatch.setattr(health, "create_engine", create)
    health.get_health_engine.cache_clear()
    try:
        health.get_health_engine()
        options = create.call_args.kwargs
        assert options["poolclass"] is health.NullPool
        assert options["connect_args"]["connect_timeout"] == 3
        assert "statement_timeout=2000" in options["connect_args"]["options"]
        assert "default_transaction_read_only=on" in options["connect_args"]["options"]
    finally:
        health.get_health_engine.cache_clear()


def test_database_unavailable_logs_no_connection_details(monkeypatch, caplog):
    engine = Mock()
    engine.connect.side_effect = OperationalError(
        "dsn=private-credential", {}, Exception("sensitive-body")
    )
    monkeypatch.setattr(health, "get_health_engine", lambda: engine)
    assert health.database_available() is False
    assert "OperationalError" in caplog.text
    assert "private-credential" not in caplog.text
    assert "sensitive-body" not in caplog.text


def test_database_readiness_runs_a_real_query(test_engine, monkeypatch):
    monkeypatch.setattr(health, "get_health_engine", lambda: test_engine)
    assert health.database_available() is True
