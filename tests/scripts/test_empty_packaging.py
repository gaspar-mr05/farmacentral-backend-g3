from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from scripts import empty_packaging as module


class FakeSession:
    def __init__(self, pending_run_id=None, unit_ids=()):
        self.pending_run_id = pending_run_id
        self.units = [SimpleNamespace(external_unit_id=value) for value in unit_ids]

    def scalar(self, statement):
        del statement
        return self.pending_run_id

    def scalars(self, statement):
        del statement
        return self.units


class FakeClient:
    async def get_spaces(self):
        return [
            {"_id": "PACKAGING", "packaging": True},
            {"_id": "BUFFER", "buffer": True, "cold": True},
        ]


@pytest.mark.anyio
async def test_empty_packaging_moves_synchronized_units(monkeypatch) -> None:
    client = FakeClient()
    session = FakeSession(unit_ids=("INTERMEDIATE-1", "KIT-1", "KIT-2"))
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
            return SimpleNamespace(moved=True)

    monkeypatch.setattr(module, "InventorySyncService", FakeSyncService)
    monkeypatch.setattr(module, "ProductMovementService", FakeMovementService)

    moved = await module.empty_packaging(client, session)

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
