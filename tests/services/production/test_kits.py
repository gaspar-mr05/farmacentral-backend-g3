from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from app.clients.farma_central import (
    FarmaCentralConnectionError,
    FarmaCentralHTTPError,
    FarmaCentralInvalidResponseError,
    FarmaCentralTimeoutError,
)
from app.models import ProductionRun, Unit
from app.schemas.farma_central import FarmaCentralProduct
from app.services.inventory.collection import InventorySnapshotChangedError
from app.services.production.kits import (
    KitProductionService,
    ProductionOutputNotLinkedError,
)
from app.services.production.orchestration import ProductionInputsUnavailableError
from app.services.production.runs import start_production_run
from tests.services.inventory.test_sync import FakeInventorySource
from tests.services.production.test_orchestration import (
    _create_inputs,
    _production_client,
)


@pytest.mark.anyio
async def test_production_sync_reloads_a_changed_inventory_snapshot(
    db_session, monkeypatch, caplog
):
    class ChangingInventory(FakeInventorySource):
        first_read = True

        async def get_space_products(self, *args, **kwargs):
            if self.first_read:
                self.first_read = False
                return []
            return await super().get_space_products(*args, **kwargs)

    client = ChangingInventory()
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.production.kits.asyncio.sleep", sleep)
    service = KitProductionService(client, db_session, raw_material_source="supply")

    await service._synchronize()

    assert db_session.scalar(select(func.count()).select_from(Unit)) == 1
    sleep.assert_awaited_once_with(1)
    assert "expected 1 units, observed 0" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        FarmaCentralConnectionError("private external body"),
        FarmaCentralTimeoutError("private external body"),
        InventorySnapshotChangedError("Inventory changed for RAW in BUFFER"),
    ],
)
@pytest.mark.parametrize("recovers", [True, False])
async def test_production_inventory_read_retries_are_bounded(
    db_session, monkeypatch, caplog, error, recovers
):
    synchronize = AsyncMock(side_effect=[error, None] if recovers else error)
    monkeypatch.setattr(
        "app.services.production.kits.InventorySyncService",
        Mock(return_value=SimpleNamespace(synchronize=synchronize)),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.production.kits.asyncio.sleep", sleep)
    client = AsyncMock()
    service = KitProductionService(client, db_session, raw_material_source="supply")

    if recovers:
        await service._synchronize()
        assert synchronize.await_count == 2
        sleep.assert_awaited_once_with(1)
    else:
        with pytest.raises(type(error)):
            await service._synchronize()
        assert synchronize.await_count == 3
        assert [call.args[0] for call in sleep.await_args_list] == [1, 2]
    assert "private external body" not in caplog.text
    client.request_products.assert_not_awaited()


@pytest.mark.anyio
async def test_permanent_inventory_validation_failure_is_not_retried(
    db_session, monkeypatch
):
    synchronize = AsyncMock(
        side_effect=FarmaCentralInvalidResponseError("Invalid units")
    )
    monkeypatch.setattr(
        "app.services.production.kits.InventorySyncService",
        Mock(return_value=SimpleNamespace(synchronize=synchronize)),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.production.kits.asyncio.sleep", sleep)
    service = KitProductionService(
        AsyncMock(), db_session, raw_material_source="supply"
    )

    with pytest.raises(FarmaCentralInvalidResponseError, match="Invalid units"):
        await service._synchronize()
    synchronize.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("recovers", [False, True])
async def test_missing_inputs_are_prepared_again_before_a_single_production_post(
    db_session, monkeypatch, caplog, recovers
):
    now = datetime.now(UTC)
    units = _create_inputs(db_session, component_sku="RAW-RETRY", count=6)
    units[-1].status = "expired"
    db_session.commit()
    client = _production_client(
        target_sku="OUTPUT-RETRY", component_sku="RAW-RETRY", now=now
    )
    kit = FarmaCentralProduct.model_validate(
        client.get_available_products.return_value[0]
    )
    service = KitProductionService(client, db_session, raw_material_source="supply")
    service._packaging_capacity = 100
    monkeypatch.setattr(service, "_reject_ambiguous_pending_runs", AsyncMock())
    ensure = AsyncMock()
    monkeypatch.setattr(service, "_ensure_available", ensure)
    monkeypatch.setattr(service, "_clear_packaging", AsyncMock())
    monkeypatch.setattr(service, "_stage_component", AsyncMock())
    monkeypatch.setattr(service, "_wait_for_linked_outputs", AsyncMock())
    monkeypatch.setattr("app.services.production.kits._wait_until", AsyncMock())
    reads = 0

    async def synchronize():
        nonlocal reads
        reads += 1
        if reads == 2 and recovers:
            units[-1].status = "available"
            db_session.commit()

    monkeypatch.setattr(service, "_synchronize", synchronize)

    if recovers:
        runs = await service._produce_quantity(kit, 3, dependency_path=())
        assert len(runs) == 1
        assert db_session.scalar(select(func.count()).select_from(ProductionRun)) == 1
        assert all(unit.status == "consumed" for unit in units)
        client.request_products.assert_awaited_once()
    else:
        with pytest.raises(ProductionInputsUnavailableError):
            await service._produce_quantity(kit, 3, dependency_path=())
        client.request_products.assert_not_awaited()
        assert db_session.scalar(select(func.count()).select_from(ProductionRun)) == 0
    assert ensure.await_count == 2
    assert "sku=OUTPUT-RETRY attempt=1/2 missing={'RAW-RETRY': 1}" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        FarmaCentralConnectionError("connection lost"),
        FarmaCentralTimeoutError("response lost"),
        FarmaCentralInvalidResponseError("Invalid production response"),
        FarmaCentralHTTPError(500),
    ],
)
async def test_uncertain_production_request_is_not_repeated(
    db_session, monkeypatch, error
):
    service = KitProductionService(
        AsyncMock(), db_session, raw_material_source="supply"
    )
    service._packaging_capacity = 100
    kit = FarmaCentralProduct.model_validate(
        {
            "sku": "KIT-UNCERTAIN",
            "name": "Kit",
            "sellable": True,
            "production": {"batch": 1, "at": "packaging"},
            "components": [{"sku": "RAW", "req": 1}],
        }
    )
    for method in (
        "_reject_ambiguous_pending_runs",
        "_ensure_available",
        "_synchronize",
        "_clear_packaging",
        "_stage_component",
    ):
        monkeypatch.setattr(service, method, AsyncMock())
    production = AsyncMock(side_effect=error)
    monkeypatch.setattr("app.services.production.kits.produce", production)

    with pytest.raises(type(error)):
        await service._produce_quantity(kit, 1, dependency_path=())

    production.assert_awaited_once()


@pytest.mark.anyio
async def test_waits_until_raw_material_units_are_visible(db_session, monkeypatch):
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
    )
    synchronize = AsyncMock()
    available_units = Mock(side_effect=[[], [], [object(), object()]])
    sleep = AsyncMock()
    monkeypatch.setattr(service, "_synchronize", synchronize)
    monkeypatch.setattr(service, "_available_units", available_units)
    monkeypatch.setattr("app.services.production.kits.asyncio.sleep", sleep)

    await service._wait_for_available_units(
        "RAW-MATERIAL",
        2,
        poll_interval_seconds=0,
    )

    assert synchronize.await_count == 3
    assert available_units.call_count == 3
    assert sleep.await_count == 2


@pytest.mark.anyio
async def test_raw_material_visibility_timeout_reports_last_quantity(
    db_session, monkeypatch
):
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
    )
    monkeypatch.setattr(service, "_synchronize", AsyncMock())
    monkeypatch.setattr(service, "_available_units", Mock(return_value=[object()]))

    with pytest.raises(
        ProductionOutputNotLinkedError,
        match=(
            "Expected 2 available units of RAW-MATERIAL, "
            "found 1 after waiting 0 seconds"
        ),
    ):
        await service._wait_for_available_units(
            "RAW-MATERIAL",
            2,
            timeout_seconds=0,
        )


@pytest.mark.anyio
async def test_waits_for_each_chunk_before_starting_the_next(db_session, monkeypatch):
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
    )
    service._packaging_capacity = 170
    product = FarmaCentralProduct.model_validate(
        {
            "sku": "FRA-SUERO-FISIO",
            "name": "Suero fisiologico",
            "production": {"batch": 10, "at": "packaging"},
            "sellable": False,
            "components": [
                {"sku": "EXC-SUERO-FISIO", "req": 15},
                {"sku": "FRA-VIDRIO-120", "req": 2},
            ],
        }
    )
    monkeypatch.setattr(service, "_reject_ambiguous_pending_runs", AsyncMock())
    monkeypatch.setattr(service, "_ensure_available", AsyncMock())
    monkeypatch.setattr(service, "_synchronize", AsyncMock())
    monkeypatch.setattr(service, "_clear_packaging", AsyncMock())
    monkeypatch.setattr(service, "_stage_component", AsyncMock())

    events = []
    now = datetime.now(UTC)
    runs = [Mock(id="run-1"), Mock(id="run-2")]
    supplies = [
        SimpleNamespace(available_at=now + timedelta(minutes=1)),
        SimpleNamespace(available_at=now + timedelta(minutes=2)),
    ]

    async def produce_chunk(*args, **kwargs):
        index = len([event for event in events if event.startswith("produce")])
        events.append(f"produce-{index + 1}")
        return runs[index], supplies[index]

    async def wait_until(available_at):
        events.append(
            f"wait-{supplies.index(SimpleNamespace(available_at=available_at)) + 1}"
        )

    async def wait_for_outputs(chunk_runs):
        events.append(f"link-{runs.index(chunk_runs[0]) + 1}")

    monkeypatch.setattr("app.services.production.kits.produce", produce_chunk)
    monkeypatch.setattr("app.services.production.kits._wait_until", wait_until)
    monkeypatch.setattr(service, "_wait_for_linked_outputs", wait_for_outputs)

    result = await service._produce_quantity(product, 20, dependency_path=())

    assert result == runs
    assert events == [
        "produce-1",
        "wait-1",
        "link-1",
        "produce-2",
        "wait-2",
        "link-2",
    ]


@pytest.mark.anyio
async def test_completed_kits_are_skipped_when_resuming(db_session, monkeypatch):
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
    )
    kit = FarmaCentralProduct.model_validate(
        {
            "sku": "KIT-COMPLETED",
            "name": "Completed kit",
            "production": {"batch": 1, "at": "packaging"},
            "sellable": True,
            "components": [{"sku": "INPUT", "req": 1}],
        }
    )
    monkeypatch.setattr(service, "_completed_quantity", Mock(return_value=30))
    produce_quantity = AsyncMock()
    monkeypatch.setattr(service, "_produce_quantity", produce_quantity)

    result = await service._produce_kits([kit], quantity_per_sku=30)

    assert result == {kit.sku: []}
    produce_quantity.assert_not_awaited()


@pytest.mark.anyio
async def test_resume_produces_only_the_missing_kit_quantity(db_session, monkeypatch):
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
    )
    kit = FarmaCentralProduct.model_validate(
        {
            "sku": "KIT-PARTIAL",
            "name": "Partial kit",
            "production": {"batch": 1, "at": "packaging"},
            "sellable": True,
            "components": [{"sku": "INPUT", "req": 1}],
        }
    )
    monkeypatch.setattr(service, "_completed_quantity", Mock(return_value=12))
    produced_runs = [Mock()]
    produce_quantity = AsyncMock(return_value=produced_runs)
    monkeypatch.setattr(service, "_produce_quantity", produce_quantity)

    result = await service._produce_kits([kit], quantity_per_sku=30)

    assert result == {kit.sku: produced_runs}
    produce_quantity.assert_awaited_once_with(kit, 18, dependency_path=())


@pytest.mark.anyio
async def test_recovers_interrupted_runs_before_planning_more_production(
    db_session, monkeypatch
):
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
    )
    now = datetime.now(UTC)
    run = start_production_run(
        db_session,
        requested_at=now,
        expected_sku="SUS-AMOXI-250",
        expected_quantity=30,
        available_at=now + timedelta(minutes=2),
    )
    wait_until = AsyncMock()
    wait_for_outputs = AsyncMock()
    monkeypatch.setattr("app.services.production.kits._wait_until", wait_until)
    monkeypatch.setattr(service, "_wait_for_linked_outputs", wait_for_outputs)

    await service._recover_pending_runs()

    wait_until.assert_awaited_once_with(run.available_at)
    wait_for_outputs.assert_awaited_once_with([run])


@pytest.mark.anyio
async def test_pending_run_recovery_is_a_noop_when_every_run_is_complete(
    db_session, monkeypatch
):
    service = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
    )
    wait_for_outputs = AsyncMock()
    monkeypatch.setattr(service, "_wait_for_linked_outputs", wait_for_outputs)

    await service._recover_pending_runs()

    wait_for_outputs.assert_not_awaited()
