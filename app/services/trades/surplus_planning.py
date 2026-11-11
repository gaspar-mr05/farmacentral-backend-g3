from collections import defaultdict
from decimal import ROUND_CEILING, Decimal
from math import ceil


class InvalidSurplusCatalogError(ValueError):
    pass


def protected_stock(catalog, stock, demand, *, raw_buffer_batches=1):
    """Allocate finished stock first, then reserve the recipe's missing inputs."""
    remaining = dict(stock)
    protected = defaultdict(int)

    def allocate(sku, quantity, path=()):
        if sku not in catalog or sku in path:
            raise InvalidSurplusCatalogError(
                "Missing or cyclic recipe; surplus is unknown"
            )
        product = catalog[sku]
        taken = min(quantity, remaining.get(sku, 0))
        protected[sku] += taken
        remaining[sku] = remaining.get(sku, 0) - taken
        missing = quantity - taken
        if product.components and missing:
            output = ceil(missing / product.production.batch) * product.production.batch
            for component in product.components:
                allocate(
                    component.sku, component.required_per_unit * output, (*path, sku)
                )
        elif missing:
            protected[sku] += missing

    for sku, quantity in sorted(demand.items()):
        allocate(sku, quantity)
    for sku, product in catalog.items():
        if product.production.at == "farma-central":
            protected[sku] += raw_buffer_batches * product.production.batch
    return dict(protected)


def replacement_cost(catalog, sku, path=()):
    if sku in path or sku not in catalog:
        raise InvalidSurplusCatalogError("Missing or cyclic replacement cost")
    product = catalog[sku]
    if product.cost is None:
        return None
    total = product.cost
    for component in product.components:
        cost = replacement_cost(catalog, component.sku, (*path, sku))
        if cost is None:
            return None
        total += cost * component.required_per_unit
    return total


def offer_price(reference, cost, minimum_margin):
    if reference <= 0 or cost < 0:
        return None
    minimum = cost / (1 - minimum_margin)
    price = max(reference, minimum, reference * Decimal("0.5")).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING
    )
    return price if price <= reference * Decimal("1.5") else None
