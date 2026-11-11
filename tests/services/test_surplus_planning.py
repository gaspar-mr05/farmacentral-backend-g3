from decimal import Decimal

import pytest

from app.services.trades.surplus_planning import (
    InvalidSurplusCatalogError,
    offer_price,
    protected_stock,
    replacement_cost,
)
from tests.services.test_procurement_planning import KIT, MID, OTHER, RAW, catalog


def test_reserves_finished_stock_before_expanding_the_missing_recipe():
    required = protected_stock(
        catalog(), {KIT: 1, MID: 1, RAW: 30, OTHER: 10}, {KIT: 3}
    )
    # One existing kit + one intermediate means only one intermediate must be made.
    assert required[KIT] == 1
    assert required[MID] == 1
    assert required[RAW] == 3  # 2 recipe units + 1 raw buffer batch.
    assert required[OTHER] == 3  # 2 units for the remaining kits + 1 raw buffer.


def test_missing_material_is_fully_protected_and_shared_components_are_not_reused():
    required = protected_stock(catalog(), {RAW: 2}, {KIT: 2})
    assert required[RAW] == 5
    assert required[OTHER] == 3


def test_price_respects_margin_and_current_market_band():
    assert offer_price(Decimal(10), Decimal(8), Decimal("0.10")) == Decimal("10.00")
    price = offer_price(Decimal(10), Decimal(12), Decimal("0.10"))
    assert price == Decimal("13.34")
    assert (price - 12) / price >= Decimal("0.10")
    assert offer_price(Decimal(10), Decimal(14), Decimal("0.10")) is None


def test_replacement_cost_includes_the_complete_recipe():
    assert replacement_cost(catalog(), KIT) == 5
    missing = catalog()
    missing[RAW] = missing[RAW].model_copy(update={"cost": None})
    assert replacement_cost(missing, KIT) is None
    with pytest.raises(InvalidSurplusCatalogError):
        protected_stock(catalog(), {}, {"MISSING": 2})
