from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Lot, Unit
from app.schemas.farma_central import FarmaCentralProduct
from app.services.production.kits import (
    KitProductionError,
    KitProductionService,
    ProductionCapacityError,
)
from app.services.production.locking import ProductionBusyError, production_lock
from app.services.production.orchestration import produce
from tests.support.recalls import RecallInventory
from tests.support.traceability import create_traceability_scenario


@pytest.mark.anyio
async def test_direct_production_respects_packaging_lock(test_engine):
    client = AsyncMock()
    with (
        Session(test_engine) as first,
        Session(test_engine) as second,
        production_lock(first),
        pytest.raises(ProductionBusyError),
    ):
        await produce(second, client=client, sku="KIT", quantity=1)
    client.get_available_products.assert_not_awaited()


@pytest.mark.anyio
async def test_recipe_failure_does_not_starve_other_kits(db_session, monkeypatch):
    service = KitProductionService(
        AsyncMock(), db_session, raw_material_source="supply"
    )
    kits = [
        FarmaCentralProduct.model_validate(
            {
                "sku": sku,
                "name": sku,
                "sellable": True,
                "production": {"batch": 1, "at": "packaging"},
                "components": [{"sku": "RAW", "req": 1}],
            }
        )
        for sku in ("KIT-A", "KIT-B")
    ]
    service._catalog = {kit.sku: kit for kit in kits}
    monkeypatch.setattr(service, "_initialize", AsyncMock())
    monkeypatch.setattr(service, "_clear_packaging", AsyncMock())
    monkeypatch.setattr(service, "_usable_stock", lambda *args: 0)
    production = AsyncMock(side_effect=[ProductionCapacityError("No capacity"), []])
    monkeypatch.setattr(service, "_produce_quantity", production)
    with pytest.raises(KitProductionError, match="KIT-A"):
        await service.maintain_kit_stock(
            minimum=1, target=2, minimum_lifetime_seconds=0
        )
    assert [call.args[0].sku for call in production.call_args_list] == [
        "KIT-A",
        "KIT-B",
    ]


def test_packaging_lock_survives_commits_and_releases_after_failure(test_engine):
    with Session(test_engine) as first, Session(test_engine) as second:
        with pytest.raises(RuntimeError, match="interrupted"), production_lock(first):
            first.commit()
            with (
                production_lock(first),
                pytest.raises(ProductionBusyError),
                production_lock(second),
            ):
                pytest.fail("Another producer acquired the lock")
            raise RuntimeError("interrupted")
        with production_lock(second):
            second.commit()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stock_status",
    [
        "available",
        "unavailable",
        "expired",
        "reserved",
        "recall_pending",
        "legacy",
        "short_lived",
    ],
)
async def test_replenishment_uses_current_traceable_stock(
    db_session, monkeypatch, stock_status
):
    scenario = create_traceability_scenario(db_session)
    lot = db_session.get(Lot, scenario.kit_lot_id)
    unit = db_session.scalar(select(Unit).where(Unit.lot_id == lot.id))
    if stock_status == "expired":
        unit.effective_expires_at = unit.effective_expires_at - timedelta(days=100)
    elif stock_status == "legacy":
        db_session.get(Lot, scenario.raw_lot_id).external_lot_id = "unreported:old"
    elif stock_status == "reserved":
        RecallInventory(db_session).order((unit, False))
        # Even an inconsistent available status cannot consume an assigned unit.
        unit.status = "available"
    elif stock_status == "short_lived":
        unit.effective_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    else:
        unit.status = stock_status
    db_session.flush()
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
        require_official_batches=True,
    )
    kit = FarmaCentralProduct.model_validate(
        {
            "sku": lot.product.sku,
            "name": "Kit",
            "sellable": True,
            "production": {"batch": 1, "at": "packaging"},
            "components": [{"sku": "RAW", "req": 1}],
        }
    )
    service._catalog = {kit.sku: kit}
    monkeypatch.setattr(service, "_initialize", AsyncMock())
    clear = AsyncMock()
    monkeypatch.setattr(service, "_clear_packaging", clear)
    produce = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "_produce_quantity", produce)

    await service.maintain_kit_stock(minimum=1, target=1, minimum_lifetime_seconds=3600)

    if stock_status in {"available", "short_lived"}:
        produce.assert_not_awaited()
    else:
        produce.assert_awaited_once_with(kit, 1, dependency_path=())
        assert clear.await_count == 2


def test_replenishment_configuration_rejects_unreachable_target():
    from tests.support.farma_central import make_settings

    with pytest.raises(ValueError, match="REPLENISHMENT_TARGET_STOCK"):
        make_settings(replenishment_minimum_stock=5, replenishment_target_stock=2)
