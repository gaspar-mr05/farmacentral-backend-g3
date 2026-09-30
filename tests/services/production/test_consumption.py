# tests/services/production/test_consumption.py
from datetime import datetime, timedelta, timezone

from app.models import CustodyEvent, CustodyEventType, Location, Lot, LotOrigin, Product, ProductCategory, Unit
from app.services.production.consumption import consume_units_for_run
from app.services.production.runs import start_production_run


def test_consume_units_for_run_marks_units_and_logs_events(db_session):
    location = Location(code="ACONDICIONAMIENTO", name="Acondicionamiento", is_refrigerated=False)
    db_session.add(location)
    db_session.flush()

    product = Product(sku="API-AMOXI-500", name="API Amoxicilina", category=ProductCategory.INSUMO, batch_size=50, requires_refrigeration=False)
    db_session.add(product)
    db_session.flush()

    lot = Lot(external_lot_id="L-API-001", product_id=product.id, origin=LotOrigin.FARMA_CENTRAL)
    db_session.add(lot)
    db_session.flush()

    now = datetime.now(timezone.utc)
    units = [
        Unit(
            external_unit_id=f"U-{i}",
            lot_id=lot.id,
            current_location_id=location.id,
            status="available",
            effective_expires_at=now + timedelta(days=100),
        )
        for i in range(12)
    ]
    db_session.add_all(units)
    db_session.flush()

    run = start_production_run(db_session, requested_at=now, expected_sku="BLI-AMOXI-500")
    consume_units_for_run(db_session, production_run_id=run.id, input_units_by_lot={lot.id: units})

    for unit in units:
        db_session.refresh(unit)
        assert unit.status == "consumed"

    events = (
        db_session.query(CustodyEvent)
        .filter(CustodyEvent.unit_id.in_([u.id for u in units]))
        .all()
    )
    assert len(events) == 12
    assert all(e.event_type == CustodyEventType.PRODUCTION_CONSUMED for e in events)