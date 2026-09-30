# tests/services/production/test_orchestration.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.models import Location, Lot, LotOrigin, Product, ProductCategory, ProductionRun, Unit
from app.services.production.orchestration import produce


@pytest.mark.anyio
async def test_produce_runs_full_flow_and_persists_locally(db_session):
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

    fake_client = AsyncMock()
    fake_client.request_fabrication_challenge.return_value = {
        "challengeId": "chal-123",
        "prefix": "abc123",
        "algorithm": "sha256-leading-zero-bits",
        "difficulty": 4,
        "sku": "BLI-AMOXI-500",
        "quantity": 3,
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }
    fake_client.request_products.return_value = {
        "sku": "BLI-AMOXI-500",
        "group": 3,
        "quantity": 3,
        "availableAt": (now + timedelta(minutes=10)).isoformat(),
    }

    run, supply = await produce(
        db_session,
        client=fake_client,
        sku="BLI-AMOXI-500",
        quantity=3,
        input_units_by_lot={lot.id: units},
    )

    assert isinstance(run, ProductionRun)
    assert supply.sku == "BLI-AMOXI-500"
    assert supply.available_at > now

    fake_client.request_fabrication_challenge.assert_awaited_once_with("BLI-AMOXI-500", 3)
    fake_client.request_products.assert_awaited_once()

    for unit in units:
        db_session.refresh(unit)
        assert unit.status == "consumed"