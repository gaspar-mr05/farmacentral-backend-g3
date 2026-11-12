from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app.models import Base
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration

PREVIOUS = "f69c14e350a3"
REVISION = "91c4e7a2d5f0"


def test_messaging_history_migration_matches_models(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with engine.connect() as connection:
        assert "messaging_messages" in inspect(connection).get_table_names()
        assert (
            compare_metadata(
                MigrationContext.configure(connection, opts={"compare_type": True}),
                Base.metadata,
            )
            == []
        )

    run_migration(engine, "downgrade", PREVIOUS)
    with engine.connect() as connection:
        assert "messaging_messages" not in inspect(connection).get_table_names()
        columns = {c["name"] for c in inspect(connection).get_columns("inbound_events")}
        assert "classified_at" not in columns

    run_migration(engine, "upgrade", REVISION)
