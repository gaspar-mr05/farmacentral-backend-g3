from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import Base, ProcurementRequest
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.services.test_trade_custody import add_market_stock


def test_procurement_migration_roundtrip_and_durable_demand(migration_database):
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
    with Session(engine) as session:
        period, _, _, _ = add_market_stock(session)
        row = ProcurementRequest(
            period_id=period.id,
            request_key="shortage",
            demand={"sku": "SOB-PAPEL-EST"},
            status="waiting",
        )
        session.add(row)
        session.commit()
        row_id = row.id
    with Session(engine) as session:
        assert session.get(ProcurementRequest, row_id).demand["sku"] == "SOB-PAPEL-EST"
    run_migration(engine, "downgrade", "c36f81a927d0")
    assert "procurement_requests" not in inspect(engine).get_table_names()
    run_migration(engine, "upgrade", "head")
    assert "procurement_requests" in inspect(engine).get_table_names()
