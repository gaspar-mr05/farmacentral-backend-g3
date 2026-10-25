from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, insert
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.main import create_app
from app.models import Lot, LotOrigin
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory


def test_deep_traceability_stays_under_ten_seconds_with_bounded_queries(
    recall_database,
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        root = inventory.lot("root")
        current = root
        for generation in range(100):
            output = inventory.lot(f"generation-{generation}")
            inventory.link(output, current)
            inventory.unit(f"unit-{generation}", output)
            current = output
        inventory.order((current.units[0], True))
        root_id, last_id, product_id = root.id, current.id, root.product_id
        session.execute(
            insert(Lot),
            [
                {
                    "id": uuid4(),
                    "external_lot_id": f"unrelated-{uuid4()}",
                    "product_id": product_id,
                    "origin": LotOrigin.OWN_PRODUCTION,
                    "expires_at": inventory.expiry,
                }
                for _ in range(2000)
            ],
        )
        session.commit()

    application = create_app()

    def session_dependency():
        with Session(recall_database) as session:
            yield session

    application.dependency_overrides[get_session] = session_dependency
    statements = []

    def record(*args):
        statements.append(args[2])

    with TestClient(application) as client:
        event.listen(recall_database, "before_cursor_execute", record)
        try:
            started = perf_counter()
            response = client.get(f"/api/traceability/{root_id}")
            elapsed = perf_counter() - started
        finally:
            event.remove(recall_database, "before_cursor_execute", record)
    assert response.status_code == 200
    assert len(response.json()["descendants"]) == 100
    assert len(response.json()["production_links"]) == 100
    assert len(response.json()["deliveries"]) == 1
    assert len(statements) <= 15
    assert elapsed < 10
    print(
        f"Traceability: generations=100 unrelated_lots=2000 "
        f"queries={len(statements)} elapsed_ms={elapsed * 1000:.2f}"
    )

    # A historical cycle terminates and never includes the queried lot as its ancestor.
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        inventory.link(session.get(Lot, root_id), session.get(Lot, last_id))
        session.commit()
    with TestClient(application) as client:
        response = client.get(f"/api/traceability/{root_id}")
    assert response.status_code == 200
    assert len(response.json()["descendants"]) == 100
    assert len(response.json()["ancestors"]) == 100
