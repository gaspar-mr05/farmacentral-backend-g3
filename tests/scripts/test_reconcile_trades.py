import hashlib
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import WorkerHeartbeat
from scripts import reconcile_trades as module
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.services.test_trade_custody import add_market_stock
from tests.support.billing import make_trade
from tests.support.farma_central import make_settings


def configure(monkeypatch, engine):
    settings = make_settings(farma_central_base_url="https://dev.example.test/")
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    for name in ("TradesClient", "PurchaseOrdersClient", "FarmaCentralClient"):
        monkeypatch.setattr(module, name, Mock(return_value=AsyncMock()))


@pytest.mark.anyio
async def test_missing_unit_evidence_is_not_reported_as_worker_success(
    migration_database, monkeypatch
):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period, _, _, _ = add_market_stock(session)
        selected = period.id
        session.add(make_trade(selected))
        session.commit()
    configure(monkeypatch, engine)
    with pytest.raises(SystemExit):
        await module.run(period_id=selected, once=True)
    with Session(engine) as session:
        row = session.get(WorkerHeartbeat, (selected, "process_trades"))
        assert row.last_success_at is None
        assert row.last_error_type == "CycleIncomplete"
        assert row.queue_depth == 1 and row.oldest_queued_at is not None


@pytest.mark.anyio
async def test_worker_once_with_no_offers_only_reads_local_state(
    migration_database, monkeypatch
):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period, _, _, _ = add_market_stock(session)
        period_id = period.id
    configure(monkeypatch, engine)
    await module.run(period_id=period_id, once=True)
    with Session(engine) as session:
        heartbeat = session.get(WorkerHeartbeat, (period_id, "process_trades"))
        assert heartbeat.last_success_at is not None
        assert heartbeat.queue_depth == 0
    for name in ("TradesClient", "PurchaseOrdersClient", "FarmaCentralClient"):
        client = getattr(module, name).return_value.__aenter__.return_value
        assert client.mock_calls == []
    # A second invocation demonstrates that the session-level lock was released.
    await module.run(period_id=period_id, once=True)


@pytest.mark.anyio
async def test_worker_refuses_concurrent_execution(migration_database, monkeypatch):
    configure(monkeypatch, migration_database)
    selected = uuid4()
    key = int.from_bytes(
        hashlib.sha256(f"trades:{selected}".encode()).digest()[:8], signed=True
    )
    with migration_database.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        connection.commit()
        try:
            with pytest.raises(ValueError, match="already running"):
                await module.run(period_id=selected, once=True)
            module.TradesClient.assert_not_called()
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()


@pytest.mark.anyio
async def test_worker_exits_on_database_failure(migration_database, monkeypatch):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period, _, _, _ = add_market_stock(session)
        selected = period.id
    configure(monkeypatch, engine)
    service = Mock(poll=AsyncMock(side_effect=SQLAlchemyError("private DB detail")))
    monkeypatch.setattr(
        module, "TradeReconciliationService", Mock(return_value=service)
    )
    with pytest.raises(SystemExit) as error:
        await module.run(period_id=selected)
    assert error.value.code == 1


def test_cli_reuses_explicit_billing_period_without_mutation_switches(monkeypatch):
    selected = uuid4()
    monkeypatch.setattr(
        module, "get_settings", lambda: make_settings(billing_period_id=str(selected))
    )
    runner = AsyncMock()
    monkeypatch.setattr(module, "run", runner)
    monkeypatch.setattr("sys.argv", ["reconcile_trades", "--once"])
    module.main()
    runner.assert_awaited_once_with(
        period_id=selected, once=True, interval=60, batch_size=50
    )
