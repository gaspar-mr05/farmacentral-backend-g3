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


def test_combines_whole_small_lots_before_publishing_a_buy_bid():
    result = planner(
        offers={
            RAW: [
                offer(RAW, 5, 2),
                offer(RAW, 8, 3, id="b" * 24),
            ]
        }
    ).plan(demand(sku=RAW, quantity=10, expected_revenue=100, max_outlay=34))
    assert result.status == "market"
    assert result.cost == 34
    assert result.consumed_cost == Decimal(34) * 10 / 13
    assert [a.quantity for a in result.actions] == [5, 8]
    assert sum(a.useful_quantity for a in result.actions) == 10
    assert result.seconds == 120


def test_larger_lot_is_valid_when_full_outlay_and_storage_fit():
    result = planner(offers={RAW: [offer(RAW, 13, 2)]}).plan(
        demand(sku=RAW, quantity=2, expected_revenue=10, max_outlay=26)
    )
    assert result.status == "market"
    assert result.cost == 26 and result.consumed_cost == 4
    assert result.actions[0].quantity == 13


@pytest.mark.parametrize(
    "limits",
    [
        {"max_lot": 12},
        {"free_space": 12},
        {"cold_space": 12},
    ],
)
def test_combined_lots_still_respect_total_quantity_and_storage(limits):
    p = planner(
        catalog={RAW: product(RAW, cold=True)},
        offers={
            RAW: [
                offer(RAW, 5, 2),
                offer(RAW, 8, 3, id="b" * 24),
            ]
        },
        **limits,
    )
    result = p.plan(demand(sku=RAW, quantity=10, expected_revenue=100, max_outlay=34))
    assert not any(a.kind == "accept" for a in result.actions)


def test_combination_cannot_spend_more_than_the_full_outlay_budget():
    p = planner(offers={RAW: [offer(RAW, 5, 2), offer(RAW, 8, 3, id="b" * 24)]})
    result = p.plan(demand(sku=RAW, quantity=10, expected_revenue=100, max_outlay=33))
    assert not any(a.kind == "accept" for a in result.actions)


def test_duplicate_offer_is_not_used_twice_to_complete_shortage():
    one = offer(RAW, 3, 1)
    result = planner(offers={RAW: [one, one]}).plan(demand(sku=RAW, quantity=5))
    assert not any(a.kind == "accept" for a in result.actions)


def test_multiple_acceptances_must_fit_remaining_delivery_time():
    p = planner(offers={RAW: [offer(RAW, 3, 1), offer(RAW, 3, 1, id="b" * 24)]})
    result = p.plan(
        demand(sku=RAW, quantity=5, deadline=datetime.now(UTC) + timedelta(seconds=90))
    )
    assert not result.actions


def test_combined_lot_surplus_can_fill_another_recipe_branch():
    products = {
        RAW: product(RAW),
        MID: product(MID, components=[(RAW, 5)]),
        KIT: product(KIT, components=[(MID, 1), (RAW, 2)]),
    }
    result = planner(
        catalog=products,
        offers={
            RAW: [
                offer(RAW, 3, 1),
                offer(RAW, 4, 1, id="b" * 24),
            ]
        },
    ).plan(demand(quantity=1, expected_revenue=100, max_outlay=9))
    assert result.status == "produce"
    assert result.cost == result.consumed_cost == 9
    assert len(result.actions) == 2
    assert sum(a.quantity for a in result.actions) == 7


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


@pytest.mark.parametrize(
    "revenue,outlay,viable",
    [
        (20, 111, True),
        (10, 111, False),
        (20, 110, False),
    ],
)
def test_intermediate_batch_cost_is_allocated_before_checking_customer_margin(
    revenue, outlay, viable
):
    products = {
        OTHER: product(OTHER, batch=10, cost=10),
        MID: product(MID, components=[(OTHER, 1)], batch=10, cost=1),
        KIT: product(KIT, components=[(MID, 1)], cost=1),
    }
    result = planner(catalog=products).plan(
        demand(
            quantity=1,
            expected_revenue=revenue,
            max_outlay=outlay,
            includes_committed=True,
            priority=1,
        )
    )
    # Buy ten raw (100), make ten intermediate (10), finish one kit (1).
    # Only one intermediate is consumed: the kit costs 10 + 1 + 1 = 12.
    if viable:
        assert result.status == "produce", result.reason
        assert result.cost == 111
        assert result.consumed_cost == 12
        assert result.actions[0].quantity == 10
    else:
        assert not result.actions


def test_intermediate_batch_bid_uses_allocated_budget_without_inventing_stock():
    products = {
        RAW: product(RAW, batch=10, cost=10),
        MID: product(MID, components=[(RAW, 1)], batch=10, cost=1),
        KIT: product(KIT, components=[(MID, 1)], cost=1),
    }
    result = planner(catalog=products).plan(
        demand(
            quantity=1,
            expected_revenue=5,
            max_outlay=31,
            includes_committed=True,
            priority=1,
        )
    )
    assert result.status == "waiting"
    assert result.cost == 31 and result.consumed_cost == 4
    assert [(a.sku, a.kind, a.quantity, a.cost) for a in result.actions] == [
        (RAW, "bid", 10, Decimal(20))
    ]


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


def test_customer_order_prefers_viable_recipe_over_waiting_for_finished_kit():
    products = {
        RAW: product(RAW, cost=1),
        KIT: product(KIT, components=[(RAW, 1)], cost=3),
    }
    source = planner(catalog=products)
    source.prices = {RAW: Decimal(2), KIT: Decimal(4)}
    pending = source.plan(
        demand(
            quantity=1,
            expected_revenue=10,
            max_outlay=20,
            includes_committed=True,
            priority=1,
        )
    )
    assert [(a.kind, a.sku) for a in pending.actions] == [("bid", RAW)]
    background = source.plan(
        demand(
            quantity=1,
            expected_revenue=10,
            max_outlay=20,
        )
    )
    assert [(a.kind, a.sku) for a in background.actions] == [("bid", KIT)]


def test_explicit_loss_limit_keeps_full_outlay_cap_and_defaults_to_zero():
    products = {OTHER: product(OTHER, cost=11)}
    strict = planner(catalog=products).plan(
        demand(
            sku=OTHER,
            quantity=1,
            expected_revenue=10,
            max_outlay=20,
            minimum_margin=0,
        )
    )
    assert not strict.actions
    permitted = planner(catalog=products).plan(
        demand(
            sku=OTHER,
            quantity=1,
            expected_revenue=10,
            max_outlay=20,
            minimum_margin=0,
            maximum_loss_fraction=Decimal("0.20"),
        )
    )
    assert permitted.status == "direct" and permitted.cost == 11
    capped = planner(catalog=products).plan(
        demand(
            sku=OTHER,
            quantity=1,
            expected_revenue=10,
            max_outlay=10,
            minimum_margin=0,
            maximum_loss_fraction=Decimal("0.20"),
        )
    )
    assert not capped.actions and "full outlay" in capped.reason


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
