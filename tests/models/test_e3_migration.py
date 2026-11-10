from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models import Base, BusinessPeriod, Order
from tests.models.test_e2_migration import (
    migration_database as migration_database,
)
from tests.models.test_e2_migration import run_migration
from tests.support.billing import make_period

PREVIOUS = "8c62e71f4b09"
REVISION = "c7b4e2a91d60"
LATER_MIGRATION_TABLES = {
    "billing_sync_states",
    "business_metric_snapshots",
}


def is_step_2_object(_object, name, *_args):
    return name not in LATER_MIGRATION_TABLES


def seed_legacy_orders(engine):
    order_id, item_id, payment_id, external_id = (uuid4() for _ in range(4))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders (id,buyer_name,buyer_email,source,status,total) "
                "VALUES (:id,'E1 buyer','old@example.com','WEB','PENDING_PAYMENT',3000)"
            ),
            {"id": order_id},
        )
        connection.execute(
            text(
                "INSERT INTO order_items (id,order_id,sku,quantity,unit_price) "
                "VALUES (:id,:order_id,'KIT',2,1500)"
            ),
            {"id": item_id, "order_id": order_id},
        )
        connection.execute(
            text(
                "INSERT INTO payments "
                "(id,order_id,external_transaction_id,amount,status) "
                "VALUES (:id,:order_id,'previous-checkout',3000,'PENDING')"
            ),
            {"id": payment_id, "order_id": order_id},
        )
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id,buyer_name,source,status,total,external_order_id) "
                "VALUES (:id,'E2 buyer','SFTP','RECEIVED',1500,"
                "'111111111111111111111111')"
            ),
            {"id": external_id},
        )
    return order_id, item_id, payment_id, external_id


def test_upgrade_and_downgrade_preserve_e1_e2_orders_and_checkout(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", PREVIOUS)
    order_id, item_id, payment_id, external_id = seed_legacy_orders(engine)

    run_migration(engine, "upgrade", REVISION)
    with Session(engine) as session:
        order = session.get(Order, order_id)
        assert order.total == Decimal("3000")
        assert isinstance(order.total, Decimal)
        assert order.items[0].id == item_id
        assert order.items[0].unit_price == Decimal("1500")
        assert order.payments[0].id == payment_id
        assert type(order.payments[0].amount) is int
        assert session.get(Order, external_id).buyer_email is None
    with engine.connect() as connection:
        assert (
            compare_metadata(
                MigrationContext.configure(
                    connection,
                    opts={
                        "compare_type": True,
                        # Later migrations are tested against current metadata
                        # separately; this test stops at the step 2 revision.
                        "include_object": is_step_2_object,
                    },
                ),
                Base.metadata,
            )
            == []
        )

    run_migration(engine, "downgrade", PREVIOUS)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT total FROM orders WHERE id=:id"), {"id": order_id}
            )
            == 3000
        )
        assert (
            connection.scalar(
                text("SELECT amount FROM payments WHERE id=:id"), {"id": payment_id}
            )
            == 3000
        )
        assert connection.scalar(text("SELECT count(*) FROM orders")) == 2
    assert "invoices" not in inspect(engine).get_table_names()
    source = next(
        col for col in inspect(engine).get_columns("orders") if col["name"] == "source"
    )
    assert "MARKET" not in source["type"].enums
    run_migration(engine, "upgrade", REVISION)
    with engine.connect() as connection:
        assert (
            compare_metadata(
                MigrationContext.configure(
                    connection,
                    opts={
                        "compare_type": True,
                        "include_object": is_step_2_object,
                    },
                ),
                Base.metadata,
            )
            == []
        )


def test_downgrade_refuses_to_delete_e3_audit_data(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", REVISION)
    with Session(engine) as session:
        period = make_period()
        session.add(period)
        session.commit()
        period_id = period.id
    with pytest.raises(AssertionError, match="Delivery 3 financial/trade data exists"):
        run_migration(engine, "downgrade", PREVIOUS)
    with Session(engine) as session:
        assert session.get(BusinessPeriod, period_id) is not None
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        )


@pytest.mark.parametrize("value", ["1500.25", "2147483648"])
@pytest.mark.parametrize(
    "table,column", [("orders", "total"), ("order_items", "unit_price")]
)
def test_downgrade_refuses_rounding_and_overflow(
    migration_database, value, table, column
):
    engine = migration_database
    run_migration(engine, "upgrade", PREVIOUS)
    order_id, item_id, _, _ = seed_legacy_orders(engine)
    run_migration(engine, "upgrade", REVISION)
    row_id = order_id if table == "orders" else item_id
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {table} SET {column}=:value WHERE id=:id"),
            {"value": Decimal(value), "id": row_id},
        )
    with pytest.raises(AssertionError, match="fractional or out-of-range"):
        run_migration(engine, "downgrade", PREVIOUS)
    with engine.connect() as connection:
        assert connection.scalar(
            text(f"SELECT {column} FROM {table} WHERE id=:id"), {"id": row_id}
        ) == Decimal(value)
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )


def test_downgrade_does_not_remove_market_orders(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", REVISION)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders (id,buyer_name,source,status,total) "
                "VALUES (:id,'Market buyer','MARKET','RECEIVED',10)"
            ),
            {"id": uuid4()},
        )
    with pytest.raises(AssertionError, match="market orders exist"):
        run_migration(engine, "downgrade", PREVIOUS)
