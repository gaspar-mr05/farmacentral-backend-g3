import pytest

from app.clients.farma_central import FarmaCentralInvalidResponseError
from app.services.inventory.collection import (
    InventoryCollector,
    InventorySnapshotChangedError,
)


class LargeInventoryClient:
    async def get_available_products(self):
        return [
            {
                "sku": "RAW-1",
                "name": "Raw material",
                "production": {"batch": 1, "at": "farma-central"},
                "sellable": False,
            }
        ]

    async def get_spaces(self):
        return [{"_id": "BUFFER", "buffer": True}]

    async def get_space_inventory(self, store_id):
        assert store_id == "BUFFER"
        return [{"sku": "RAW-1", "quantity": 250}]

    async def get_space_products(self, store_id, sku, *, limit=None):
        assert (store_id, sku, limit) == ("BUFFER", "RAW-1", 200)
        return [
            {
                "_id": f"UNIT-{index}",
                "sku": sku,
                "store": store_id,
                "expiresAt": "2027-01-01T00:00:00Z",
            }
            for index in range(200)
        ]


@pytest.mark.anyio
async def test_collection_marks_groups_larger_than_api_limit_as_incomplete() -> None:
    _, _, units, incomplete_groups = await InventoryCollector(
        LargeInventoryClient()
    ).collect()

    assert len(units) == 200
    assert incomplete_groups == {("BUFFER", "RAW-1")}


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["count", "sku", "space"])
async def test_collection_distinguishes_changed_counts_from_invalid_origin(change):
    class ChangedInventory(LargeInventoryClient):
        async def get_space_products(self, *args, **kwargs):
            units = await super().get_space_products(*args, **kwargs)
            if change == "count":
                return units[:-1]
            units[0]["sku" if change == "sku" else "store"] = "UNEXPECTED"
            return units

    with pytest.raises(FarmaCentralInvalidResponseError) as caught:
        await InventoryCollector(ChangedInventory()).collect()

    assert isinstance(caught.value, InventorySnapshotChangedError) == (
        change == "count"
    )
    assert "RAW-1" in str(caught.value) and "BUFFER" in str(caught.value)
    if change == "count":
        assert "expected 200 units, observed 199" in str(caught.value)
