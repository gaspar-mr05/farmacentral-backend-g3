"""Compare complete acquisition costs; recipes are substitutes, never disassembly."""

from math import ceil

from app.schemas.procurement import (
    ProcurementAction,
    ProcurementDemand,
    ProcurementPlan,
)

# Only these seven inputs have the E3 selective-access matrix.
GROUP3_DIRECT = {"EXC-LACTOSA-DC", "LAM-BLISTER-PVC", "EXC-SUERO-FISIO"}
GROUP3_MARKET = {"SOB-PAPEL-EST", "FRA-VIDRIO-120", "VIA-VIDRIO-10", "EXC-JARABE-BASE"}


def direct_allowed(product, group_id):
    if product.production.at != "farma-central":
        return False
    if group_id == 3 and product.sku in GROUP3_MARKET:
        return False
    if product.production.groups is not None:
        return str(group_id) in product.production.groups
    if product.sku in GROUP3_DIRECT | GROUP3_MARKET:
        return group_id == 3 and product.sku in GROUP3_DIRECT
    return True


class ProcurementPlanner:
    def __init__(
        self,
        catalog,
        prices,
        offers,
        stock,
        incoming,
        *,
        group_id,
        now,
        free_space,
        cold_space,
        max_lot=200,
        pending_skus=frozenset(),
    ):
        self.pending_skus = pending_skus
        self.catalog, self.prices, self.offers = catalog, prices, offers
        self.stock, self.incoming = stock, incoming
        self.group_id, self.now = group_id, now
        self.free_space, self.cold_space, self.max_lot = free_space, cold_space, max_lot

    def plan(self, demand: ProcurementDemand) -> ProcurementPlan:
        self.budget = demand.budget
        available = dict(self.stock)
        available.update(
            {("incoming", sku): qty for sku, (qty, _) in self.incoming.items()}
        )
        plan, _ = self._node(
            demand.sku,
            demand.quantity + demand.buffer,
            available,
            (),
            demand.deadline,
        )
        if plan is None:
            return ProcurementPlan(
                status="waiting",
                reason="No route with known cost, capacity and deadline",
            )
        if plan.cost > demand.budget:
            return ProcurementPlan(
                status="waiting",
                reason="Expected margin does not cover the complete acquisition cost",
            )
        if plan.seconds > (demand.deadline - self.now).total_seconds():
            return ProcurementPlan(
                status="waiting",
                reason="Insufficient time for acquisition and remaining fabrication",
            )
        return plan

    def _node(self, sku, quantity, stock, path, deadline):
        if sku in path or sku not in self.catalog:
            return None, stock
        product = self.catalog[sku]
        free = min(quantity, stock.get(sku, 0))
        stock[sku] = stock.get(sku, 0) - free
        missing = quantity - free
        if not missing:
            return ProcurementPlan(status="ready"), stock
        if sku in self.pending_skus:
            return ProcurementPlan(
                status="waiting", reason="Market purchase awaiting confirmation/receipt"
            ), stock
        incoming = min(missing, stock.get(("incoming", sku), 0))
        stock[("incoming", sku)] = stock.get(("incoming", sku), 0) - incoming
        missing -= incoming
        incoming_seconds = self.incoming.get(sku, (0, 0))[1]
        if not missing:
            return ProcurementPlan(
                status="waiting",
                seconds=incoming_seconds,
                reason="Confirmed supply/production pending",
            ), stock
        options = []
        make = None
        branch_stock = stock
        for offer in self.offers.get(sku, []):
            if (
                offer.side != "sell"
                or offer.status != "open"
                or offer.publisher_group in {None, self.group_id}
                or offer.expires_at is None
                or (offer.expires_at - self.now).total_seconds() < 60
                or offer.quantity < missing
                or offer.quantity
                > min(self.max_lot, max(2 * missing, product.production.batch))
            ):
                continue
            options.append(
                ProcurementPlan(
                    status="market",
                    seconds=60,
                    cost=offer.unit_price * offer.quantity,
                    actions=[
                        ProcurementAction(
                            kind="accept",
                            sku=sku,
                            quantity=offer.quantity,
                            useful_quantity=missing,
                            cost=offer.unit_price * offer.quantity,
                            offer_id=offer.id,
                        )
                    ],
                )
            )
        batch_quantity = (
            ceil(missing / product.production.batch) * product.production.batch
        )
        if (
            direct_allowed(product, self.group_id)
            and product.cost is not None
            and product.production.time is not None
            and batch_quantity <= min(5000, self.max_lot)
        ):
            options.append(
                ProcurementPlan(
                    status="direct",
                    cost=product.cost * batch_quantity,
                    seconds=60 * product.production.time,
                    actions=[
                        ProcurementAction(
                            kind="direct",
                            sku=sku,
                            quantity=batch_quantity,
                            useful_quantity=missing,
                            cost=product.cost * batch_quantity,
                        )
                    ],
                )
            )
        if (
            product.production.at == "packaging"
            and product.components
            and product.cost is not None
            and product.production.time is not None
            and (not product.storage.cold or self.cold_space >= batch_quantity)
        ):
            branch_stock = dict(stock)
            children = []
            for component in product.components:
                child, branch_stock = self._node(
                    component.sku,
                    component.required_per_unit * batch_quantity,
                    branch_stock,
                    (*path, sku),
                    deadline,
                )
                if child is None:
                    break
                children.append(child)
            else:
                pending = any(c.status == "waiting" for c in children)
                make = ProcurementPlan(
                    status="waiting" if pending else "produce",
                    cost=product.cost * batch_quantity + sum(c.cost for c in children),
                    seconds=60 * product.production.time
                    + sum(c.seconds for c in children),
                    actions=[a for c in children for a in c.actions],
                )
                options.append(make)
        # A bid is a fallback, not confirmed stock or a faster viable route.
        price = self.prices.get(sku)
        if price and batch_quantity <= self.max_lot:
            options.append(
                ProcurementPlan(
                    status="waiting",
                    cost=price * batch_quantity,
                    seconds=3600,
                    actions=[
                        ProcurementAction(
                            kind="bid",
                            sku=sku,
                            quantity=batch_quantity,
                            useful_quantity=missing,
                            cost=price * batch_quantity,
                        )
                    ],
                    reason="No suitable supply; publish a buy offer",
                )
            )
        if incoming:
            for option in options:
                option.status = "waiting"
                option.seconds = max(option.seconds, incoming_seconds)
                option.reason = (
                    "Partial confirmed supply pending; acquire only the remainder"
                )
        feasible = [
            p
            for p in options
            if p.cost <= self.budget
            and self._fits(p)
            and p.seconds <= (deadline - self.now).total_seconds()
        ]
        if not feasible:
            return None, stock
        chosen = min(feasible, key=lambda p: (p.status == "waiting", p.cost, p.seconds))
        if chosen is make:
            return chosen, branch_stock
        return chosen, stock

    def _fits(self, plan):
        ids = [a.offer_id for a in plan.actions if a.offer_id]
        if len(ids) != len(set(ids)):
            return False  # The same public offer cannot fill two recipe branches.
        total = sum(a.quantity for a in plan.actions)
        cold = sum(a.quantity for a in plan.actions if self.catalog[a.sku].storage.cold)
        return total <= self.free_space and cold <= self.cold_space
