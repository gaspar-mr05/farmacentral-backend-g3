from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models import MarketOffer
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.services.test_trade_custody import add_market_stock


def test_market_reconciliation_migration_roundtrip_preserves_offers(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", "head")
    with Session(engine) as session:
        period, _, _, units = add_market_stock(session)
        offer = MarketOffer(
            period_id=period.id,
            external_offer_id="a" * 24,
            side="sell",
            publisher_group_id=3,
            sku=units[0].lot.product.sku,
            quantity=2,
            unit_price=1,
            status="open",
        )
        session.add(offer)
        session.commit()
        offer_id = offer.id
    names = {column["name"] for column in inspect(engine).get_columns("market_offers")}
    assert {
        "last_checked_at",
        "next_retry_at",
        "reconciled_at",
        "attempt_count",
        "last_error",
        "http_status",
    } <= names
    with engine.connect() as connection:
        before = connection.scalar(text("SELECT count(*) FROM market_offers"))
    run_migration(engine, "downgrade", "b24d6e830f19")
    names = {column["name"] for column in inspect(engine).get_columns("market_offers")}
    assert "last_checked_at" not in names
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM market_offers")) == before
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM market_offers WHERE id=:id"), {"id": offer_id}
            )
            == "open"
        )
    run_migration(engine, "upgrade", "head")
    assert "last_checked_at" in {
        column["name"] for column in inspect(engine).get_columns("market_offers")
    }
