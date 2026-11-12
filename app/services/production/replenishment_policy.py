from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil

from app.services.supply.planning import direct_allowed
from app.services.trades.surplus_planning import replacement_cost


def stock_targets(
    product,
    catalog,
    *,
    recent_quantity,
    minimum,
    target,
    minimum_lifetime,
    group_id,
    price,
):
    rate = recent_quantity / 6

    def lead(sku, path=()):
        if sku in path or sku not in catalog:
            raise ValueError("Missing or cyclic replenishment recipe")
        p = catalog[sku]
        own = (p.production.time or 1) / 60
        if p.production.at == "farma-central":
            return own if direct_allowed(p, group_id) else 1
        return own + sum(lead(c.sku, (*path, sku)) for c in p.components)

    batch = product.production.batch
    lifetime = product.obsolescence or max(2, minimum_lifetime / 3600)
    consumption_cap = max(batch, ceil(max(rate, minimum / 2) * lifetime / 2))
    lower = max(
        batch,
        min(consumption_cap, max(minimum, ceil(rate * max(1, lead(product.sku))))),
    )
    upper = max(
        lower,
        min(max(target, 2 * ceil(rate * max(1, lead(product.sku)))), consumption_cap),
    )
    cost = replacement_cost(catalog, product.sku)
    if price is not None and cost is not None and cost >= price * Decimal("0.9"):
        # Expensive recipes justify a smaller buffer, not forcing every kit to
        # one unit and making each minimum supply lot fund itself from one sale.
        useful_batch = max(
            (
                ceil(catalog[c.sku].production.batch / c.required_per_unit)
                for c in product.components
            ),
            default=batch,
        )
        upper = min(upper, max(lower, useful_batch))
    return min(200, lower), min(200, upper)


def schedule_key(priority, deadline, last_attempt, missing, sku):
    urgent = deadline is not None and deadline <= datetime.now(UTC) + timedelta(
        minutes=15
    )
    return (
        priority,
        not urgent,
        last_attempt or datetime.min.replace(tzinfo=UTC),
        deadline or datetime.max.replace(tzinfo=UTC),
        -missing,
        sku,
    )
