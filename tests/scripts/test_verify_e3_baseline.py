import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import InternalError, OperationalError

from app.clients.farma_central import FarmaCentralHTTPError
from scripts import verify_e3_baseline as module
from tests.services.inventory.test_sync import FakeInventorySource
from tests.support.farma_central import make_settings


def test_configuration_report_never_exposes_credentials_or_url_queries():
    settings = make_settings(
        database_url="postgresql+psycopg://private-user:private-key@localhost/db",
        farma_central_base_url="https://private-user:private-key@dev.test/api/?key=secret",
        purchase_orders_base_url="https://private-user:private-key@dev.test/oc/",
    )
    report = module.configuration_summary(settings)
    assert report["database"] == {"host": "localhost", "port": 5432, "name": "db"}
    serialized = json.dumps(report)
    assert all(
        value not in serialized for value in ("private-user", "private-key", "secret")
    )


def test_database_failure_hides_dsn_and_driver_error(monkeypatch):
    engine = Mock()
    engine.connect.side_effect = OperationalError(
        "private-key", {}, Exception("secret")
    )
    monkeypatch.setattr(module, "create_engine", Mock(return_value=engine))

    report = module.check_database(make_settings())

    assert report["status"] == "failed"
    assert report["error_type"] == "OperationalError"
    assert "private-key" not in str(report) and "secret" not in str(report)
    engine.dispose.assert_called_once()


def test_baseline_snapshot_is_read_only_and_does_not_apply_migrations(test_engine):
    with test_engine.connect() as connection:
        report = module.database_snapshot(connection)
        assert report["status"] == "failed"  # Test schema has no Alembic history.
        assert report["read_only"] is True
        assert report["current"] == []
        assert "counts" not in report
        with pytest.raises(InternalError):
            connection.execute(
                text("CREATE TABLE forbidden_baseline_write (id integer)")
            )


def test_current_migration_snapshot_reports_counts_in_read_only_mode(test_engine):
    config = Config(str(module.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(module.ROOT / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    with test_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num varchar(32))")
        )
        connection.execute(
            text("INSERT INTO alembic_version VALUES (:head)"), {"head": head}
        )
    try:
        with test_engine.connect() as connection:
            report = module.database_snapshot(connection)
        assert report["status"] == "passed"
        assert report["read_only"] is True
        assert set(report["counts"]) == {
            "products",
            "lots",
            "units",
            "production_runs",
            "recalls",
        }
        assert "orders" in report and "inbound_events" in report
    finally:
        with test_engine.begin() as connection:
            connection.execute(text("DROP TABLE alembic_version"))


@pytest.mark.anyio
async def test_official_inventory_probe_reads_counts_without_mutating_units():
    client = FakeInventorySource()
    client.request_products = AsyncMock()
    client.move_product = AsyncMock()

    report = await module.inventory_snapshot(client)

    assert report["status"] == "passed"
    assert sum(s["quantities"].get(client.sku, 0) for s in report["spaces"]) == 1
    assert client.requested_limits == []  # No per-unit scan, let alone writes.
    client.request_products.assert_not_awaited()
    client.move_product.assert_not_awaited()


@pytest.mark.anyio
async def test_inventory_error_logs_only_type_and_http_status(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_available_products.side_effect = FarmaCentralHTTPError(
        429, "private-body"
    )
    monkeypatch.setattr(module, "FarmaCentralClient", Mock(return_value=client))

    report = await module.check_inventory(make_settings())

    assert report["status"] == "failed" and report["http_status"] == 429
    assert "private-body" not in str(report)


@pytest.mark.anyio
async def test_default_report_skips_external_probes_and_preserves_limits(monkeypatch):
    monkeypatch.setattr(module, "check_database", lambda _: {"status": "failed"})
    inventory = AsyncMock()
    http = AsyncMock()
    services = AsyncMock()
    monkeypatch.setattr(module, "check_inventory", inventory)
    monkeypatch.setattr(module, "check_http", http)
    monkeypatch.setattr(module, "check_systemd", services)
    args = SimpleNamespace(inventory=False, api_url=None, check_services=False)

    report = await module.run(args, make_settings())

    assert report["scope"] == "e3_step1_observation_only"
    assert report["llm_calls"] == report["business_mutations"] == 0
    assert report["checks"]["database"]["status"] == "failed"
    assert report["not_verified"]
    inventory.assert_not_awaited()
    http.assert_not_awaited()
    services.assert_not_awaited()
