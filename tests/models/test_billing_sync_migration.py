import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models import Base, BillingSyncState
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.support.billing import make_period


def test_sync_migration_matches_models_and_can_reverse_when_unused(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with engine.connect() as connection:
        assert (
            compare_metadata(
                MigrationContext.configure(connection, opts={"compare_type": True}),
                Base.metadata,
            )
            == []
        )
    run_migration(engine, "downgrade", "c7b4e2a91d60")
    assert "billing_sync_states" not in inspect(engine).get_table_names()
    assert "invoices" in inspect(engine).get_table_names()
    run_migration(engine, "upgrade", "head")
    assert "billing_sync_states" in inspect(engine).get_table_names()


def test_sync_downgrade_preserves_existing_progress(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period = make_period()
        session.add(period)
        session.flush()
        session.add(BillingSyncState(period_id=period.id, stream="client:pending"))
        session.commit()
    with pytest.raises(AssertionError, match="reconciliation progress exists"):
        run_migration(engine, "downgrade", "c7b4e2a91d60")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM billing_sync_states")) == 1
