"""Compare acquisition routes without confusing useful consumption with outlay."""

from decimal import ROUND_DOWN, Decimal
from math import ceil

from app.core.scarcity import GROUP3_DIRECT, GROUP3_MARKET
from app.schemas.procurement import (
    ProcurementAction,
    ProcurementDemand,
    ProcurementPlan,
)
from app.services.trades.surplus_planning import replacement_cost


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
        stock_unit_costs=None,
    ):
        self.pending_skus = pending_skus
        self.catalog, self.prices, self.offers = catalog, prices, offers
        self.stock, self.incoming = stock, incoming
        self.group_id, self.now = group_id, now
        self.free_space, self.cold_space, self.max_lot = free_space, cold_space, max_lot
        self.stock_unit_costs = stock_unit_costs or {}

    def plan(self, demand: ProcurementDemand) -> ProcurementPlan:
        self.budget = demand.budget
        self.outlay_budget = demand.outlay_budget
        self.allocate_costs = demand.max_outlay is not None
        self.blockers = []
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
                reason="; ".join(dict.fromkeys(self.blockers))
                or f"{demand.sku}: no route with known cost, capacity and deadline",
            )
        return plan

    def _unit_cost(self, sku):
        if sku in self.stock_unit_costs:
            return self.stock_unit_costs[sku]
        return replacement_cost(self.catalog, sku)

    def _economic_cost(self, plan):
        return plan.consumed_cost if self.allocate_costs else plan.cost

    def _node(self, sku, quantity, stock, path, deadline):
        if sku in path or sku not in self.catalog:
            self.blockers.append(f"{sku}: missing or cyclic recipe")
            return None, stock
        product = self.catalog[sku]
        free = min(quantity, stock.get(sku, 0))
        stock[sku] = stock.get(sku, 0) - free
        free_cost = Decimal(0)
        if self.allocate_costs and free:
            unit_cost = stock.get(("unit_cost", sku))
            if unit_cost is None:
                unit_cost = self._unit_cost(sku)
            if unit_cost is None:
                self.blockers.append(f"{sku}: unknown unit cost for usable stock")
                return None, stock
            free_cost = unit_cost * free
        missing = quantity - free
        if not missing:
            return ProcurementPlan(status="ready", consumed_cost=free_cost), stock
        if sku in self.pending_skus:
            pending_cost = self._unit_cost(sku) if self.allocate_costs else Decimal(0)
            if pending_cost is None:
                self.blockers.append(
                    f"{sku}: pending acquisition has unknown unit cost"
                )
                return None, stock
            return ProcurementPlan(
                status="waiting",
                consumed_cost=free_cost + missing * pending_cost,
                reason="Market purchase awaiting confirmation/receipt",
            ), stock
        incoming = min(missing, stock.get(("incoming", sku), 0))
        stock[("incoming", sku)] = stock.get(("incoming", sku), 0) - incoming
        missing -= incoming
        if self.allocate_costs and incoming:
            unit_cost = self._unit_cost(sku)
            if unit_cost is None:
                self.blockers.append(
                    f"{sku}: confirmed production has unknown unit cost"
                )
                return None, stock
            free_cost += unit_cost * incoming
        incoming_seconds = self.incoming.get(sku, (0, 0))[1]
        if not missing:
            return ProcurementPlan(
                status="waiting",
                consumed_cost=free_cost,
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
                    consumed_cost=free_cost + offer.unit_price * missing,
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
                    consumed_cost=free_cost + product.cost * missing,
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
                    consumed_cost=free_cost
                    + (
                        product.cost * batch_quantity
                        + sum(c.consumed_cost or Decimal(0) for c in children)
                    )
                    * Decimal(missing)
                    / batch_quantity,
                    seconds=60 * product.production.time
                    + sum(c.seconds for c in children),
                    actions=[a for c in children for a in c.actions],
                )
                options.append(make)
        # A bid is a fallback, not confirmed stock or a faster viable route.
        price = self.prices.get(sku)
        if price and self.allocate_costs:
            # Bid no more than the useful demand can afford, while respecting
            # the market's reference-price band and paying the whole lot.
            proposed = min(
                price,
                (self.budget - free_cost) / missing,
                self.outlay_budget / batch_quantity,
            ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if proposed < price * Decimal("0.5"):
                self.blockers.append(
                    f"{sku}: affordable bid {proposed} below market floor "
                    f"{price * Decimal('0.5')}"
                )
                price = None
            else:
                price = proposed
        if price and batch_quantity <= self.max_lot:
            options.append(
                ProcurementPlan(
                    status="waiting",
                    cost=price * batch_quantity,
                    consumed_cost=free_cost + price * missing,
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
        feasible = []
        for option in options:
            causes = []
            if self._economic_cost(option) > self.budget:
                causes.append(
                    f"consumption cost {self._economic_cost(option)} "
                    f"exceeds {self.budget}"
                )
            if option.cost > self.outlay_budget:
                causes.append(f"full outlay {option.cost} exceeds {self.outlay_budget}")
            if not self._fits(option):
                causes.append(
                    f"capacity/offer conflict (free={self.free_space}, "
                    f"cold={self.cold_space})"
                )
            if option.seconds > (deadline - self.now).total_seconds():
                causes.append(
                    f"{option.seconds:g}s acquisition/production misses deadline"
                )
            if causes:
                self.blockers.append(f"{sku} ({option.status}): {', '.join(causes)}")
            else:
                feasible.append(option)
        if not feasible:
            if not options:
                self.blockers.append(
                    f"{sku}: no usable offer/recipe; cost={product.cost}, "
                    f"time={product.production.time}, batch={batch_quantity}, "
                    f"cold_space={self.cold_space}"
                )
            return None, stock
        chosen = min(
            feasible,
            key=lambda p: (
                p.status == "waiting",
                self._economic_cost(p),
                p.cost,
                p.seconds,
            ),
        )
        remaining = branch_stock if chosen is make else stock
        if self.allocate_costs:
            acquired = (
                batch_quantity
                if chosen is make
                else chosen.actions[0].quantity
                if chosen.actions
                else missing
            )
            # Only virtual planning stock; bids and future manufacture never
            # become physical stock or confirmed customer capacity here.
            remaining[sku] = remaining.get(sku, 0) + acquired - missing
            remaining[("unit_cost", sku)] = (chosen.consumed_cost - free_cost) / missing
        return chosen, remaining

    def _fits(self, plan):
        ids = [a.offer_id for a in plan.actions if a.offer_id]
        if len(ids) != len(set(ids)):
            return False  # The same public offer cannot fill two recipe branches.
        total = sum(a.quantity for a in plan.actions)
        cold = sum(a.quantity for a in plan.actions if self.catalog[a.sku].storage.cold)
        return total <= self.free_space and cold <= self.cold_space
