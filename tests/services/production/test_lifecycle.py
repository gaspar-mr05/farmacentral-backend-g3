# tests/services/production/test_lifecycle.py
from datetime import UTC, datetime

from app.db.production_runs import find_pending_runs_for_sku
from app.models import Product, ProductCategory
from app.services.production.runs import finish_production_run, start_production_run


def test_finish_production_run_links_correct_pending_run_by_sku(db_session):
    output_product = Product(
        sku="BLI-AMOXI-500",
        name="Blíster amoxicilina",
        category=ProductCategory.ACONDICIONADO,
        batch_size=3,
        requires_refrigeration=False,
    )
    db_session.add(output_product)
    db_session.flush()

    now = datetime.now(UTC)
    run = start_production_run(
        db_session,
        requested_at=now,
        expected_sku="BLI-AMOXI-500",
        expected_quantity=3,
        available_at=now,
    )

    found = find_pending_runs_for_sku(db_session, sku="BLI-AMOXI-500")[0]
    assert found.id == run.id

    output_lot = finish_production_run(
        db_session,
        run=found,
        output_lot_external_id="L-BLI-REAL-001",
        output_product_id=output_product.id,
        completed_at=now,
    )

    db_session.refresh(run)
    assert run.output_lot_id == output_lot.id
    assert run.completed_at is not None

    # ya no debería aparecer como pendiente
    assert find_pending_runs_for_sku(db_session, sku="BLI-AMOXI-500") == []
