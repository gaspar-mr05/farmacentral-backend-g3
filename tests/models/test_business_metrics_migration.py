import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models import Base, BusinessMetricSnapshot
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.support.billing import make_period


def test_metrics_migration_matches_models_and_reverses_when_empty(migration_database):
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
    run_migration(engine, "downgrade", "d83f92a16b40")
    assert "business_metric_snapshots" not in inspect(engine).get_table_names()


def test_metrics_downgrade_preserves_snapshots(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period = make_period()
        session.add(period)
        session.flush()
        session.add(
            BusinessMetricSnapshot(
                period_id=period.id,
                commit_sha="abc123",
                metrics={"margin": "0"},
            )
        )
        session.commit()
    with pytest.raises(AssertionError, match="must be preserved"):
        run_migration(engine, "downgrade", "d83f92a16b40")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT count(*) FROM business_metric_snapshots"))
            == 1
        )
