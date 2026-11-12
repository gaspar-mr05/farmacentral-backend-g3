from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from app.models import Base
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration


def test_recall_evidence_migration_preserves_existing_recalls(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", "91c4e7a2d5f0")
    recall_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO recalls "
                "(id,recall_id,batch,sku,reason,issued_at,deadline) "
                "VALUES (:id,'recall-history','official-batch','RAW','Quality',"
                "now(),now()+interval '30 minutes')"
            ),
            {"id": recall_id},
        )
    run_migration(engine, "upgrade", "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM recalls")) == 1
        assert (
            connection.scalar(text("SELECT investigation_evidence FROM recalls"))
            is None
        )
        assert (
            compare_metadata(
                MigrationContext.configure(connection, opts={"compare_type": True}),
                Base.metadata,
            )
            == []
        )
    run_migration(engine, "downgrade", "91c4e7a2d5f0")
    assert "investigation_evidence" not in {
        column["name"] for column in inspect(engine).get_columns("recalls")
    }
    run_migration(engine, "upgrade", "head")
