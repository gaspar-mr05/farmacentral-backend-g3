import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Integer, MetaData, create_engine, delete, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.base import Base
from app.models import InboundEventStatus, Order
from tests.support.orders import make_external_order

PREVIOUS_REVISION = "7e3a8b91f2c4"
REVISION = "3f7c2d91a8b4"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def migration_database(test_engine):
    schema = f"migration_{uuid4().hex}"
    admin_engine = create_engine(test_engine.url)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    url = test_engine.url.update_query_dict(
        {"options": f"-csearch_path={schema} -ctimezone=UTC"}
    )
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def run_migration(engine, command, revision):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_migration_preserves_web_orders_and_allows_durable_event_linking(
    migration_database,
):
    engine = migration_database
    run_migration(engine, "upgrade", PREVIOUS_REVISION)
    web_order_id = uuid4()
    web_item_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO orders "
                "(id, buyer_name, buyer_email, source, status, total) "
                "VALUES (:id, 'Cliente anterior', 'anterior@example.com', "
                "'WEB', 'PAID', 1500)"
            ),
            {"id": web_order_id},
        )
        connection.execute(
            text(
                "INSERT INTO order_items (id, order_id, sku, quantity, unit_price) "
                "VALUES (:id, :order_id, 'KIT-ANALGESIA', 1, 1500)"
            ),
            {"id": web_item_id, "order_id": web_order_id},
        )

    run_migration(engine, "upgrade", REVISION)
    with Session(engine) as session:
        old_order = session.get(Order, web_order_id)
        assert old_order.buyer_email == "anterior@example.com"
        assert old_order.external_order_id is None
        assert old_order.items[0].id == web_item_id
        event_id = uuid4()
        session.execute(
            text(
                "INSERT INTO inbound_events (id,source,external_event_id,payload) "
                "VALUES (:id,'SFTP','orders.xml','{\"orderId\": \"pending\"}')"
            ),
            {"id": event_id},
        )
        session.commit()

    with pytest.raises(
        AssertionError, match="Cannot downgrade while Delivery 2 data exists"
    ):
        run_migration(engine, "downgrade", PREVIOUS_REVISION)

    with Session(engine) as session:
        assert (
            session.scalar(
                text("SELECT order_id FROM inbound_events WHERE id=:id"),
                {"id": event_id},
            )
            is None
        )
        assert (
            session.scalar(
                text("SELECT status FROM inbound_events WHERE id=:id"),
                {"id": event_id},
            )
            == InboundEventStatus.PENDING.name
        )
        order = make_external_order()
        session.add(order)
        session.flush()
        session.execute(
            text("UPDATE inbound_events SET order_id=:order_id WHERE id=:event_id"),
            {"order_id": order.id, "event_id": event_id},
        )
        session.commit()
        order_id = order.id

    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT order_id FROM inbound_events WHERE id=:id"),
                {"id": event_id},
            )
            == order_id
        )
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "include_object": lambda obj, name, kind, reflected, compare_to: (
                    (kind != "table" or name in {"orders", "inbound_events"})
                    and not (
                        getattr(getattr(obj, "table", None), "name", None)
                        == "inbound_events"
                        and name
                        in {
                            "classified_at",
                            "consulted_external_order_id",
                            "resolution_evidence",
                            "order_status_snapshot",
                        }
                    )
                ),
            },
        )
        # Compare the E2 revision with its monetary type, not E3's later NUMERIC.
        legacy_metadata = MetaData()
        for name in ("orders", "inbound_events"):
            Base.metadata.tables[name].to_metadata(legacy_metadata)
        legacy_metadata.tables["orders"].c.total.type = Integer()
        assert compare_metadata(context, legacy_metadata) == []

    with pytest.raises(
        AssertionError, match="Cannot downgrade while Delivery 2 data exists"
    ):
        run_migration(engine, "downgrade", PREVIOUS_REVISION)
    with engine.begin() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        connection.execute(text("DELETE FROM inbound_events"))
        connection.execute(
            text("DELETE FROM order_items WHERE order_id = :id"), {"id": order_id}
        )
        connection.execute(delete(Order).where(Order.id == order_id))

    run_migration(engine, "downgrade", PREVIOUS_REVISION)
    with engine.connect() as connection:
        assert "inbound_events" not in inspect(connection).get_table_names()
        assert "external_order_id" not in {
            column["name"] for column in inspect(connection).get_columns("orders")
        }
        assert connection.scalar(text("SELECT count(*) FROM orders")) == 1

    run_migration(engine, "upgrade", REVISION)
    with Session(engine) as session:
        assert session.get(Order, web_order_id).items[0].id == web_item_id


def test_unique_event_constraint_handles_concurrent_deliveries(migration_database):
    engine = migration_database
    run_migration(engine, "upgrade", REVISION)
    ready = Barrier(2)

    def record_event():
        with engine.connect() as connection:
            ready.wait(timeout=5)
            try:
                connection.execute(
                    text(
                        "INSERT INTO inbound_events "
                        "(id,source,external_event_id,payload) "
                        "VALUES (:id,'PUBSUB','same-message',"
                        '\'{"orderId": "pending"}\')'
                    ),
                    {"id": uuid4()},
                )
                connection.commit()
            except IntegrityError as error:
                connection.rollback()
                assert (
                    error.orig.diag.constraint_name
                    == "uq_inbound_events_source_external_id"
                )
                return "duplicate"
            return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(record_event)
        second = executor.submit(record_event)
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [
            "created",
            "duplicate",
        ]
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM inbound_events")) == 1
