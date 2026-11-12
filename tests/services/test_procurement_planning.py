from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.schemas.farma_central import FarmaCentralProduct
from app.schemas.procurement import ProcurementDemand
from app.schemas.trades import TradeOffer
from app.services.supply.planning import ProcurementPlanner, direct_allowed

RAW = "SOB-PAPEL-EST"
MID = "SOB-GASA-EST"
KIT = "KIT-CURACION"
OTHER = "EXC-SUERO-FISIO"


def product(sku, *, components=(), batch=1, cold=False, cost=1, time=1, groups=None):
    return FarmaCentralProduct.model_validate(
        {
            "sku": sku,
            "name": sku,
            "sellable": sku.startswith("KIT-"),
            "cost": cost,
            "production": {
                "at": "packaging" if components else "farma-central",
                "batch": batch,
                "time": time,
                "groups": groups,
            },
            "storage": {"cold": cold},
            "components": [{"sku": s, "req": q} for s, q in components],
        }
    )


def catalog():
    return {
        RAW: product(RAW),
        OTHER: product(OTHER),
        MID: product(MID, components=[(RAW, 2)]),
        KIT: product(KIT, components=[(MID, 1), (OTHER, 1)]),
    }


def offer(sku, quantity, price, *, publisher=5, state="open", minutes=30, id="a" * 24):
    now = datetime.now(UTC)
    return TradeOffer(
        id=id,
        sku=sku,
        side="sell",
        quantity=quantity,
        unitPrice=price,
        state=state,
        publisher=str(publisher),
        createdAt=now,
        expiresAt=now + timedelta(minutes=minutes),
    )


def demand(**changes):
    return ProcurementDemand(
        sku=changes.pop("sku", KIT),
        quantity=changes.pop("quantity", 2),
        deadline=changes.pop("deadline", datetime.now(UTC) + timedelta(hours=2)),
        expected_revenue=changes.pop("expected_revenue", 100),
        **changes,
    )


def planner(**changes):
    return ProcurementPlanner(
        changes.pop("catalog", catalog()),
        {RAW: Decimal(2)},
        changes.pop("offers", {}),
        changes.pop("stock", {}),
        changes.pop("incoming", {}),
        group_id=3,
        now=datetime.now(UTC),
        free_space=changes.pop("free_space", 100),
        cold_space=changes.pop("cold_space", 100),
        **changes,
    )


@pytest.mark.parametrize(
    "sku", [RAW, "FRA-VIDRIO-120", "VIA-VIDRIO-10", "EXC-JARABE-BASE"]
)
def test_group3_restricted_material_is_never_direct_even_with_old_catalog(sku):
    assert not direct_allowed(product(sku, groups=["3", "4"]), 3)


@pytest.mark.parametrize("sku", [OTHER, "EXC-LACTOSA-DC", "LAM-BLISTER-PVC"])
def test_group3_direct_critical_materials_follow_matrix(sku):
    assert direct_allowed(product(sku), 3)


def test_compares_raw_conditioned_and_finished_kit_without_disassembly():
    quoted = {RAW: [offer(RAW, 4, 2)], MID: [offer(MID, 2, 4)], KIT: [offer(KIT, 2, 5)]}
    result = planner(offers=quoted).plan(demand())
    assert result.status == "market"
    assert result.cost == 10
    assert [(a.sku, a.quantity) for a in result.actions] == [(KIT, 2)]
    del quoted[KIT]
    result = planner(offers=quoted).plan(demand())
    assert any(a.sku == MID and a.kind == "accept" for a in result.actions)
    assert not any(a.sku == RAW for a in result.actions)


def test_net_shortage_uses_stock_buffer_and_confirmed_production():
    result = planner(stock={KIT: 2}).plan(demand(buffer=1))
    # One missing kit -> only two paper units, not the complete three-kit demand.
    assert [(a.sku, a.quantity) for a in result.actions if a.kind == "bid"] == [
        (RAW, 2)
    ]
    result = planner(incoming={KIT: (2, 120)}).plan(demand())
    assert result.status == "waiting"
    assert result.actions == []


def test_full_offer_cost_and_moderate_lots_prevent_false_bargains():
    result = planner(
        offers={RAW: [offer(RAW, 8, 1), offer(RAW, 4, Decimal("1.5"), id="b" * 24)]}
    ).plan(demand(sku=RAW, quantity=4))
    assert result.cost == 6
    assert result.actions[0].offer_id == "b" * 24
    result = planner(offers={RAW: [offer(RAW, 200, Decimal("0.01"))]}).plan(
        demand(sku=RAW, quantity=4)
    )
    assert result.actions[0].kind == "bid"


@pytest.mark.parametrize(
    "kwargs", [{"publisher": 3}, {"state": "expired"}, {"minutes": 0}]
)
def test_own_expired_or_nearly_expired_offers_are_not_purchased(kwargs):
    result = planner(offers={RAW: [offer(RAW, 4, 1, **kwargs)]}).plan(
        demand(sku=RAW, quantity=4)
    )
    assert result.actions[0].kind == "bid"


def test_budget_deadline_capacity_and_cold_are_mandatory():
    source = {KIT: [offer(KIT, 2, 5)]}
    assert not planner(offers=source).plan(demand(expected_revenue=1)).actions
    assert (
        not planner(offers=source)
        .plan(demand(deadline=datetime.now(UTC) + timedelta(seconds=20)))
        .actions
    )
    assert not planner(offers=source, free_space=0).plan(demand()).actions
    products = catalog()
    products[KIT] = products[KIT].model_copy(
        update={"storage": products[KIT].storage.model_copy(update={"cold": True})}
    )
    assert (
        not planner(catalog=products, offers=source, cold_space=0)
        .plan(demand())
        .actions
    )


def test_direct_supply_respects_batch_and_catalog_access():
    result = planner(catalog={OTHER: product(OTHER, batch=10)}).plan(
        demand(sku=OTHER, quantity=3)
    )
    assert result.actions[0].kind == "direct"
    assert result.actions[0].quantity == 10
    assert result.cost == 10
    assert not direct_allowed(product(OTHER, groups=["4"]), 3)


def test_existing_costs_are_subtracted_before_authorizing_new_acquisition():
    result = planner(offers={KIT: [offer(KIT, 2, 5)]}).plan(demand(other_costs=95))
    assert result.status == "waiting"
    assert result.actions == []


def test_buy_fallback_never_outlives_remaining_delivery_window():
    result = planner().plan(
        demand(sku=RAW, quantity=4, deadline=datetime.now(UTC) + timedelta(minutes=30))
    )
    assert result.status == "waiting"
    assert result.actions == []


def test_partial_confirmed_supply_only_buys_the_net_remainder():
    result = planner(incoming={RAW: (2, 120)}, offers={RAW: [offer(RAW, 3, 1)]}).plan(
        demand(sku=RAW, quantity=5)
    )
    assert result.status == "waiting"
    assert result.actions[0].quantity == 3
    assert result.actions[0].useful_quantity == 3
    assert result.cost == 3
    assert result.seconds == 120


def test_useful_consumption_and_full_lot_outlay_are_separate():
    products = {OTHER: product(OTHER, batch=10, cost=2)}
    result = planner(catalog=products).plan(
        demand(
            sku=OTHER,
            quantity=1,
            expected_revenue=5,
            max_outlay=20,
        )
    )
    assert result.status == "direct"
    assert result.cost == 20 and result.consumed_cost == 2
    assert result.actions[0].quantity == 10
    denied = planner(catalog=products).plan(
        demand(
            sku=OTHER,
            quantity=1,
            expected_revenue=5,
            max_outlay=19,
        )
    )
    assert not denied.actions
    assert OTHER in denied.reason and "full outlay 20 exceeds 19" in denied.reason


def test_production_allocation_includes_all_stages_and_keeps_full_outlay():
    products = {
        OTHER: product(OTHER, batch=10, cost=2),
        MID: product(MID, components=[(OTHER, 1)], batch=4, cost=1),
        KIT: product(KIT, components=[(MID, 1)], cost=1),
    }
    result = planner(catalog=products).plan(
        demand(
            quantity=1,
            expected_revenue=10,
            max_outlay=30,
        )
    )
    # Buy 10 raw (20), manufacture 4 intermediate (4), finish 1 kit (1).
    # Consumption is raw 2 + intermediate manufacture 1 + kit manufacture 1.
    assert result.status == "produce"
    assert result.cost == 25 and result.consumed_cost == 4


def test_shared_recipe_branches_reuse_lot_surplus_without_double_purchase():
    products = {
        OTHER: product(OTHER, batch=10, cost=2),
        MID: product(MID, components=[(OTHER, 1)], cost=1),
        KIT: product(KIT, components=[(MID, 1), (OTHER, 1)], cost=1),
    }
    result = planner(catalog=products).plan(
        demand(
            quantity=1,
            expected_revenue=10,
            max_outlay=25,
        )
    )
    assert result.cost == 22 and result.consumed_cost == 6
    assert len(result.actions) == 1 and result.actions[0].quantity == 10


def test_expensive_usable_stock_is_not_treated_as_free_in_new_cost_policy():
    products = {
        OTHER: product(OTHER, cost=8),
        KIT: product(KIT, components=[(OTHER, 1)], cost=3),
    }
    result = planner(catalog=products, stock={OTHER: 1}).plan(
        demand(
            quantity=1,
            expected_revenue=10,
            max_outlay=100,
        )
    )
    assert not result.actions
    assert "consumption cost 11" in result.reason


def test_cheaper_known_market_stock_cost_can_replace_catalog_estimate():
    products = {
        OTHER: product(OTHER, cost=8),
        KIT: product(KIT, components=[(OTHER, 1)], cost=3),
    }
    result = planner(
        catalog=products,
        stock={OTHER: 1},
        stock_unit_costs={OTHER: Decimal(1)},
    ).plan(demand(quantity=1, expected_revenue=10, max_outlay=100))
    assert result.status == "produce"
    assert result.cost == 3 and result.consumed_cost == 4


def test_spending_full_budget_does_not_erase_cost_of_stock_or_allow_more_outlay():
    products = {
        OTHER: product(OTHER, cost=2),
        KIT: product(KIT, components=[(OTHER, 1)], cost=1),
    }
    result = planner(catalog=products, stock={OTHER: 1}).plan(
        demand(
            quantity=1,
            expected_revenue=10,
            other_costs=20,
            max_outlay=21,
        )
    )
    assert result.cost == 1 and result.consumed_cost == 3
    denied = planner(catalog=products, stock={OTHER: 1}).plan(
        demand(
            quantity=1,
            expected_revenue=10,
            other_costs=21,
            max_outlay=21,
        )
    )
    assert not denied.actions and "full outlay" in denied.reason


def test_buy_bid_uses_affordable_price_inside_market_band():
    result = planner(catalog={RAW: product(RAW)}).plan(
        demand(
            sku=RAW,
            quantity=1,
            expected_revenue=Decimal("1.50"),
            max_outlay=10,
        )
    )
    assert result.actions[0].kind == "bid"
    assert result.cost == result.consumed_cost == Decimal("1.35")
    denied = planner(catalog={RAW: product(RAW)}).plan(
        demand(
            sku=RAW,
            quantity=1,
            expected_revenue=Decimal("0.50"),
            max_outlay=10,
        )
    )
    assert not denied.actions and "below market floor" in denied.reason


def test_waiting_offers_still_do_not_become_stock_in_allocated_policy():
    result = planner(pending_skus={RAW}).plan(
        demand(
            sku=RAW,
            quantity=1,
            expected_revenue=10,
            max_outlay=20,
        )
    )
    assert result.status == "waiting" and result.actions == []
