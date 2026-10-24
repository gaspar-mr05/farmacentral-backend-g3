from uuid import uuid4

from sqlalchemy import event, insert
from sqlalchemy.orm import Session

from app.models import Lot, LotOrigin, ProductionInput, ProductionRun, Unit
from app.services.recalls import RecallInvestigationService
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory


def test_investigation_uses_bounded_queries_with_large_unrelated_history(
    recall_database,
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        root = inventory.lot("affected-0")
        previous = root
        delivered = []
        for depth in range(101):
            if depth:
                lot = inventory.lot(f"affected-{depth}")
                inventory.link(lot, previous)
                previous = lot
            for number in range(3):
                unit = inventory.unit(f"affected-unit-{depth}-{number}", previous)
                if depth in {0, 50, 100} and number == 0:
                    delivered.append((unit, True))
        inventory.order(*delivered)
        recall = inventory.recall(root)
        recall_id = recall.recall_id
        unrelated_ids = [uuid4() for _ in range(2000)]
        session.execute(
            insert(Lot),
            [
                {
                    "id": lot_id,
                    "external_lot_id": f"unrelated-{number}",
                    "product_id": root.product_id,
                    "origin": LotOrigin.OWN_PRODUCTION,
                    "expires_at": inventory.expiry,
                }
                for number, lot_id in enumerate(unrelated_ids)
            ],
        )
        session.execute(
            insert(Unit),
            [
                {
                    "id": uuid4(),
                    "external_unit_id": f"unrelated-unit-{number}-{item}",
                    "lot_id": lot_id,
                    "current_location_id": inventory.locations["storage"].id,
                    "status": "available",
                    "effective_expires_at": inventory.expiry,
                }
                for number, lot_id in enumerate(unrelated_ids)
                for item in range(10)
            ],
        )
        runs = [uuid4() for _ in unrelated_ids[1:]]
        session.execute(
            insert(ProductionRun),
            [
                {
                    "id": run_id,
                    "output_lot_id": lot_id,
                    "expected_sku": root.product.sku,
                    "expected_quantity": 1,
                    "requested_at": inventory.now,
                    "completed_at": inventory.now,
                }
                for run_id, lot_id in zip(runs, unrelated_ids[1:], strict=True)
            ],
        )
        session.execute(
            insert(ProductionInput),
            [
                {
                    "id": uuid4(),
                    "production_run_id": run_id,
                    "input_lot_id": lot_id,
                    "quantity_consumed": 1,
                }
                for run_id, lot_id in zip(runs, unrelated_ids[:-1], strict=True)
            ],
        )
        session.commit()

    statements = []

    def count_sql(connection, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip()
            .upper()
            .startswith(("SELECT", "WITH", "INSERT", "UPDATE"))
        ):
            statements.append(statement)

    event.listen(recall_database, "before_cursor_execute", count_sql)
    try:
        with Session(recall_database) as session:
            result = RecallInvestigationService(session).investigate(recall_id)
    finally:
        event.remove(recall_database, "before_cursor_execute", count_sql)

    assert len(result.lots) == 101
    assert len(result.stock_units) == 300
    assert len(result.orders) == 1 and len(result.orders[0].deliveries) == 3
    assert len(statements) <= 20  # Does not grow by generation or by affected unit.
    assert result.elapsed_ms < 5000
    print(
        f"Recall benchmark: lots=2101 units=20303 history_links=2099 "
        f"affected_lots=101 affected_stock=300 "
        f"statements={len(statements)} elapsed_ms={result.elapsed_ms:.2f}"
    )
