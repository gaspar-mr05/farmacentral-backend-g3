import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import Base, LotIdentityRepair, Unit
from tests.models.test_e2_migration import (
    migration_database as migration_database,
)
from tests.models.test_e2_migration import (
    run_migration,
)
from tests.support.recalls import RecallInventory


def test_lot_identity_audit_migration_preserves_inventory(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", "2a17c9e603b8")
    with Session(engine) as session:
        inventory = RecallInventory(session)
        lot = inventory.lot("old")
        unit = inventory.unit("old-unit", lot)
        session.commit()
        unit_id = unit.id

    run_migration(engine, "upgrade", "head")
    with engine.connect() as connection:
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )
    with Session(engine) as session:
        assert session.get(Unit, unit_id) is not None

    run_migration(engine, "downgrade", "2a17c9e603b8")
    assert "lot_identity_repairs" not in inspect(engine).get_table_names()
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        session.add(
            LotIdentityRepair(
                unit_id=unit_id,
                previous_batch="unreported:old-unit",
                official_batch="L-OLD",
            )
        )
        session.commit()
    with pytest.raises(AssertionError, match="Cannot drop lot identity repair audit"):
        run_migration(engine, "downgrade", "2a17c9e603b8")
