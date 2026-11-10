import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import BillingSyncState
from scripts import reconcile_billing as module
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.services.test_billing_reconciliation import BillingSource
from tests.support.billing import make_period
from tests.support.farma_central import make_settings


def worker_environment(monkeypatch, engine, **overrides):
    settings = make_settings(
        farma_central_base_url="https://dev.example.test/", **overrides
    )
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    return settings


@pytest.mark.anyio
async def test_worker_once_persists_observations_and_releases_lock(
    migration_database, monkeypatch
):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period = make_period(starts_at=datetime.now(UTC) - timedelta(days=1))
        session.add(period)
        session.commit()
        period_id = period.id
    worker_environment(monkeypatch, engine)
    source = BillingSource()
    context = AsyncMock()
    context.__aenter__.return_value = source
    monkeypatch.setattr(module, "BillingClient", Mock(return_value=context))
    sales_factory = Mock()
    monkeypatch.setattr(module, "SalesInvoicingService", sales_factory)

    await module.run(period_id=period_id, once=True)
    await module.run(period_id=period_id, once=True)

    with Session(engine) as session:
        states = session.scalars(select(BillingSyncState)).all()
        assert len(states) == 5
        assert all(state.last_success_at is not None for state in states)
    assert source.bank_calls == 1
    assert len(source.calls) == 8
    factory = module.BillingReconciliationService
    monkeypatch.setattr(
        module,
        "BillingReconciliationService",
        Mock(side_effect=SQLAlchemyError("private-db-error")),
    )
    with pytest.raises(SystemExit) as stopped:
        await module.run(period_id=period_id)
    assert stopped.value.code == 1
    monkeypatch.setattr(module, "BillingReconciliationService", factory)
    await module.run(period_id=period_id, once=True)
    assert len(source.calls) == 12
    sales_factory.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("failed_reads", [0, 1])
async def test_worker_emission_requires_opt_in_and_successful_reconciliation(
    migration_database, monkeypatch, failed_reads
):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period = make_period(starts_at=datetime.now(UTC) - timedelta(days=1))
        session.add(period)
        session.commit()
        period_id = period.id
    worker_environment(monkeypatch, engine)
    billing_context, orders_context = AsyncMock(), AsyncMock()
    monkeypatch.setattr(module, "BillingClient", Mock(return_value=billing_context))
    monkeypatch.setattr(
        module, "PurchaseOrdersClient", Mock(return_value=orders_context)
    )
    reader = Mock(
        poll=AsyncMock(
            return_value={"queries": 4, "observations": 0, "failed": failed_reads}
        )
    )
    monkeypatch.setattr(
        module, "BillingReconciliationService", Mock(return_value=reader)
    )
    emitter = Mock(
        poll=AsyncMock(return_value={"confirmed": 1, "missing": 0, "uncertain": 0})
    )
    factory = Mock(return_value=emitter)
    monkeypatch.setattr(module, "SalesInvoicingService", factory)
    if failed_reads:
        with pytest.raises(SystemExit):
            await module.run(period_id=period_id, once=True, emit_sales=True)
        emitter.poll.assert_not_awaited()
    else:
        await module.run(period_id=period_id, once=True, emit_sales=True)
        emitter.poll.assert_awaited_once()
        assert factory.call_args.args[:2] == (
            billing_context.__aenter__.return_value,
            orders_context.__aenter__.return_value,
        )


@pytest.mark.anyio
async def test_worker_does_not_overlap_an_existing_process(
    migration_database, monkeypatch
):
    engine = migration_database
    worker_environment(monkeypatch, engine)
    period_id = uuid4()
    key = int.from_bytes(
        hashlib.sha256(f"billing:{period_id}".encode()).digest()[:8], signed=True
    )
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        connection.commit()
        try:
            with pytest.raises(ValueError, match="already running"):
                await module.run(period_id=period_id, once=True)
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            connection.commit()


@pytest.mark.anyio
@pytest.mark.parametrize("overrides", [{"environment": "prod"}, {"group_id": 4}])
async def test_worker_rejects_wrong_accounting_scope_before_soap(
    migration_database, monkeypatch, overrides
):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period = make_period(**overrides)
        session.add(period)
        session.commit()
        period_id = period.id
    worker_environment(monkeypatch, engine)
    factory = Mock()
    monkeypatch.setattr(module, "BillingClient", factory)
    with pytest.raises(ValueError, match="environment/group"):
        await module.run(period_id=period_id, once=True)
    factory.assert_not_called()
