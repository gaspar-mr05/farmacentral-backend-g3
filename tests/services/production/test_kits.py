from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.schemas.farma_central import FarmaCentralProduct
from app.services.production.kits import (
    KitProductionService,
    ProductionOutputNotLinkedError,
)


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
