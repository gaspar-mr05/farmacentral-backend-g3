from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from scripts import empty_packaging as module


def _product(sku: str, *, sellable: bool) -> dict:
    return {
        "sku": sku,
        "name": sku,
        "production": {"batch": 1, "at": "packaging"},
        "sellable": sellable,
    }


def _unit(unit_id: str, sku: str) -> dict:
    return {
        "_id": unit_id,
        "sku": sku,
        "store": "PACKAGING",
        "expiresAt": "2027-01-01T00:00:00Z",
    }


class FakeSession:
    def __init__(self, pending_run_id=None):
        self.pending_run_id = pending_run_id

    def scalar(self, statement):
        del statement
        return self.pending_run_id


class FakeClient:
    def __init__(self):
        self.units = {
            "INTERMEDIATE": [_unit("INTERMEDIATE-1", "INTERMEDIATE")],
            "KIT-NEW": [_unit("KIT-1", "KIT-NEW"), _unit("KIT-2", "KIT-NEW")],
        }

    async def get_available_products(self):
        return [
            _product("INTERMEDIATE", sellable=False),
            _product("KIT-NEW", sellable=True),
        ]

    async def get_spaces(self):
        return [
            {"_id": "PACKAGING", "packaging": True},
            {"_id": "BUFFER", "buffer": True, "cold": True},
        ]

    async def get_space_products(self, store_id, sku, *, limit=None):
        assert store_id == "PACKAGING"
        assert limit == module.PAGE_SIZE
        return list(self.units[sku])


@pytest.mark.anyio
async def test_empty_packaging_discovers_and_moves_kits(monkeypatch) -> None:
    client = FakeClient()
    sync = AsyncMock()
    moves = []

    class FakeSyncService:
        def __init__(self, received_client, session):
            assert received_client is client

        synchronize = sync

    class FakeMovementService:
        def __init__(self, received_client, session):
            assert received_client is client

        async def move(self, *, product_id, destination_store):
            assert destination_store == "BUFFER"
            moves.append(product_id)
            for units in client.units.values():
                units[:] = [unit for unit in units if unit["_id"] != product_id]
            return SimpleNamespace(moved=True)

    monkeypatch.setattr(module, "InventorySyncService", FakeSyncService)
    monkeypatch.setattr(module, "ProductMovementService", FakeMovementService)

    moved = await module.empty_packaging(client, FakeSession())

    assert moved == 3
    assert moves == ["INTERMEDIATE-1", "KIT-1", "KIT-2"]
    assert sync.await_count == 2


@pytest.mark.anyio
async def test_empty_packaging_rejects_pending_production() -> None:
    with pytest.raises(module.PendingProductionRunsError):
        await module.empty_packaging(FakeClient(), FakeSession("pending-run"))


def test_single_space_rejects_ambiguous_roles() -> None:
    spaces = [
        module.FarmaCentralSpace.model_validate({"_id": "BUFFER-1", "buffer": True}),
        module.FarmaCentralSpace.model_validate({"_id": "BUFFER-2", "buffer": True}),
    ]

    with pytest.raises(FarmaCentralInvalidResponseError):
        module._single_space(spaces, role="buffer")
