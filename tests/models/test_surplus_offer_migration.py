from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import Base, MarketOffer
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.services.test_trade_custody import add_market_stock


def test_surplus_audit_migration_preserves_offers_and_matches_models(
    migration_database,
):
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
        period, _, _, units = add_market_stock(session)
        row = MarketOffer(
            period_id=period.id,
            side="sell",
            publisher_group_id=3,
            sku=units[0].lot.product.sku,
            quantity=2,
            unit_price=5,
            status="open",
            strategy="surplus",
            estimated_unit_cost=2,
            protected_quantity=1,
        )
        session.add(row)
        session.commit()
        offer_id = row.id
    run_migration(engine, "downgrade", "d47a92c138e1")
    assert "strategy" not in {
        c["name"] for c in inspect(engine).get_columns("market_offers")
    }
    with engine.connect() as connection:
        from sqlalchemy import text

        assert (
            connection.scalar(
                text("SELECT quantity FROM market_offers WHERE id=:id"),
                {"id": offer_id},
            )
            == 2
        )
    run_migration(engine, "upgrade", "head")
    assert "source_location_id" in {
        c["name"] for c in inspect(engine).get_columns("market_offer_reservations")
    }
