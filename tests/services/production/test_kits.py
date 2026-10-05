from unittest.mock import AsyncMock, Mock

import pytest

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
