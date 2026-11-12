from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.production.replenishment_policy import schedule_key, stock_targets
from tests.services.test_procurement_planning import product


def targets(catalog, *, consumption=0, price=10):
    return stock_targets(
        catalog["KIT-A"],
        catalog,
        recent_quantity=consumption,
        minimum=2,
        target=6,
        minimum_lifetime=3600,
        group_id=3,
        price=Decimal(price),
    )


def test_buffers_increase_with_consumption_and_respect_lifetime_and_margin():
    raw = product("RAW", time=10)
    kit = product("KIT-A", components=[("RAW", 1)], time=5)
    catalog = {"RAW": raw, "KIT-A": kit}
    base = targets(catalog)
    busy = targets(catalog, consumption=60)
    assert busy[0] > base[0] and busy[1] > base[1]
    kit.obsolescence = 1
    assert targets(catalog, consumption=60)[1] < busy[1]
    assert (
        targets(catalog, consumption=60, price=2)[0]
        == targets(catalog, consumption=60)[0]
    )


def test_expensive_recipes_keep_useful_buffers_instead_of_collapsing_to_one():
    catalog = {
        "RAW": product("RAW", batch=4, cost=100),
        "KIT-A": product("KIT-A", components=[("RAW", 1)], cost=100),
    }
    catalog["KIT-A"].obsolescence = 48
    minimum, target = targets(catalog, price=150)
    assert minimum == 2 and target == 4


def test_shared_market_scarcity_increases_lead_time_protection():
    direct = product("RAW", time=1)
    restricted = product("SOB-PAPEL-EST", time=1)
    a = {"RAW": direct, "KIT-A": product("KIT-A", components=[("RAW", 1)], time=1)}
    b = {
        restricted.sku: restricted,
        "KIT-A": product("KIT-A", components=[(restricted.sku, 1)], time=1),
    }
    a["KIT-A"].obsolescence = b["KIT-A"].obsolescence = 12
    assert targets(b, consumption=60)[0] > targets(a, consumption=60)[0]


def test_same_priority_gives_other_kits_a_turn_without_overriding_urgent_orders():
    now = datetime.now(UTC)
    deadline = now + timedelta(hours=1)
    recent = schedule_key(2, deadline, now, 8, "KIT-A")
    untouched = schedule_key(2, deadline, None, 1, "KIT-B")
    urgent = schedule_key(2, now + timedelta(minutes=5), now, 1, "KIT-C")
    accepted = schedule_key(0, deadline, now, 1, "KIT-D")
    assert accepted < urgent < untouched < recent
