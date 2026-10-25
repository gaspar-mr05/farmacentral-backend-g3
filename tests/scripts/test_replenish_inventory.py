from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.services.production.locking import ProductionBusyError
from scripts import replenish_inventory as module
from tests.support.farma_central import make_settings


@pytest.mark.anyio
async def test_worker_recovers_next_cycle_and_closes_resources(monkeypatch, caplog):
    client = AsyncMock()
    client.__aenter__.return_value = client
    session = MagicMock()
    service = Mock()
    service.maintain_kit_stock = AsyncMock(
        side_effect=[RuntimeError("private external body"), {}]
    )
    settings = make_settings(replenishment_poll_interval_seconds=3)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "FarmaCentralClient", lambda **kwargs: client)
    monkeypatch.setattr(module, "SessionLocal", Mock(return_value=session))
    monkeypatch.setattr(module, "KitProductionService", Mock(return_value=service))
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopAsyncIteration

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(StopAsyncIteration):
        await module.run()
    assert service.maintain_kit_stock.await_count == 2
    assert sleeps == [3, 3]
    assert "private external body" not in caplog.text
    assert session.__exit__.call_count == 2
    client.__aexit__.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("busy", [False, True])
async def test_once_reports_failure_but_treats_busy_packaging_as_deferred(
    monkeypatch, busy
):
    client = AsyncMock()
    session = MagicMock()
    service = Mock()
    service.maintain_kit_stock = AsyncMock(
        side_effect=ProductionBusyError() if busy else RuntimeError()
    )
    monkeypatch.setattr(module, "get_settings", make_settings)
    monkeypatch.setattr(module, "FarmaCentralClient", lambda **kwargs: client)
    monkeypatch.setattr(module, "SessionLocal", Mock(return_value=session))
    monkeypatch.setattr(module, "KitProductionService", Mock(return_value=service))
    if busy:
        await module.run(once=True)
    else:
        with pytest.raises(SystemExit) as exc:
            await module.run(once=True)
        assert exc.value.code == 1
