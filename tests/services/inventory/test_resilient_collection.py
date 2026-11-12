import pytest

from app.clients.farma_central import FarmaCentralInvalidResponseError
from app.services.inventory.collection import InventoryCollector


class RacingInventory:
    async def get_available_products(self):
        return [
            {
                "sku": sku,
                "name": sku,
                "sellable": False,
                "production": {"at": "farma-central", "batch": 1},
            }
            for sku in ("MOVING", "STABLE")
        ]

    async def get_spaces(self):
        return [{"_id": "STORE"}, {"_id": "BUFFER", "buffer": True}]

    async def get_space_inventory(self, space):
        return (
            [{"sku": "MOVING", "quantity": 1}, {"sku": "STABLE", "quantity": 1}]
            if space == "STORE"
            else []
        )

    async def get_space_products(self, space, sku, **kwargs):
        if sku == "MOVING":
            return []
        return [
            {
                "_id": "stable",
                "sku": sku,
                "store": space,
                "expiresAt": "2027-01-01T00:00:00Z",
                "batch": "L-STABLE",
            }
        ]


@pytest.mark.anyio
async def test_unstable_group_does_not_abort_unrelated_inventory():
    _, _, units, incomplete = await InventoryCollector(
        RacingInventory(), tolerate_changes=True
    ).collect()
    assert [u.external_id for u in units] == ["stable"]
    assert incomplete == {("STORE", "MOVING"), ("BUFFER", "MOVING")}


@pytest.mark.anyio
async def test_invalid_identity_is_never_accepted_by_resilient_mode():
    class WrongOrigin(RacingInventory):
        async def get_space_products(self, space, sku, **kwargs):
            units = await super().get_space_products(space, sku, **kwargs)
            for unit in units:
                unit["store"] = "WRONG"
            return units

    with pytest.raises(FarmaCentralInvalidResponseError):
        await InventoryCollector(WrongOrigin(), tolerate_changes=True).collect()


def test_unverified_units_are_not_treated_as_missing_or_available(db_session):
    from app.db.units import upsert_units
    from app.schemas.units import UnitData
    from tests.services.test_trade_custody import add_market_stock

    _, storage, _, units = add_market_stock(db_session, quantity=1)
    unit = units[0]
    lot = unit.lot
    upsert_units(
        db_session,
        [],
        {lot.external_lot_id: lot},
        {storage.code: storage},
        incomplete_groups={(storage.code, lot.product.sku)},
        uncertain_missing=True,
    )
    assert unit.status == "inventory_uncertain"
    upsert_units(
        db_session,
        [
            UnitData(
                external_unit_id=unit.external_unit_id,
                lot_external_id=lot.external_lot_id,
                location_code=storage.code,
                status="available",
                effective_expires_at=unit.effective_expires_at,
            )
        ],
        {lot.external_lot_id: lot},
        {storage.code: storage},
        uncertain_missing=True,
    )
    assert unit.status == "available"
