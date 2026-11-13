"""Bounded sourcing decisions, separate from the packaging lock and producer."""

import asyncio
import hashlib
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import TypeAdapter
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.clients.trades import TradesHTTPError
from app.models import (
    Location,
    Lot,
    MarketOffer,
    MarketOfferReservation,
    MarketOfferSide,
    Order,
    OrderItem,
    OrderStatus,
    ProcurementRequest,
    Product,
    ProductionRun,
    ReplenishmentState,
    Unit,
)
from app.queries.recalls import blocked_lots
from app.schemas.farma_central import FarmaCentralProduct, FarmaCentralSpace
from app.schemas.procurement import ProcurementDemand, ProcurementPlan
from app.schemas.trades import TradeOfferCreate
from app.services.inventory.sync import InventorySyncService
from app.services.production.lineage import has_official_lineage
from app.services.trades.custody import TradeCustodyService
from app.services.trades.surplus_planning import protected_stock, replacement_cost

from .planning import ProcurementPlanner


class ProcurementBusyError(Exception):
    pass


@contextmanager
def procurement_lock(session, period_id):
    key = int.from_bytes(
        hashlib.sha256(f"procurement:{period_id}".encode()).digest()[:8], signed=True
    )
    with session.get_bind().engine.connect() as connection:
        locked = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        connection.commit()
        if not locked:
            raise ProcurementBusyError("Another sourcing decision is in progress")
        try:
            yield
        finally:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
                connection.commit()
            except Exception:
                connection.invalidate()
                raise


class ScarcityProcurementService:
    def __init__(
        self,
        inventory,
        trades,
        orders,
        prices,
        session,
        period,
        *,
        tolerant_inventory=False,
    ):
        self.tolerant_inventory = tolerant_inventory
        self.inventory, self.trades, self.prices = inventory, trades, prices
        self.session, self.period = session, period
        self.custody = TradeCustodyService(
            session,
            farma_central=inventory,
            trades=trades,
            purchase_orders=orders,
            period=period,
        )

    async def preview(
        self, demand: ProcurementDemand, *, replacing_offer_id=None, snapshot=None
    ) -> ProcurementPlan:
        if snapshot is not None:
            catalog, spaces, prices = snapshot
        else:
            sync = InventorySyncService(self.inventory, self.session)
            if self.tolerant_inventory:
                await sync.synchronize(tolerate_changes=True)
            else:
                await sync.synchronize()
            catalog_payload, spaces_payload, price_payload = await asyncio.gather(
                self.inventory.get_available_products(),
                self.inventory.get_spaces(),
                self.prices.get_current_prices(use_cache=False),
            )
            catalog = {
                p.sku: p
                for p in TypeAdapter(list[FarmaCentralProduct]).validate_python(
                    catalog_payload
                )
            }
            spaces = TypeAdapter(list[FarmaCentralSpace]).validate_python(
                spaces_payload
            )
            # Malformed or missing prices cannot authorize a bid.
            prices = {}
            for row in price_payload:
                value = Decimal(str(row.get("price", 0)))
                if value.is_finite() and value > 0:
                    prices[row["sku"]] = value
        now = datetime.now(UTC)
        eligible_locations = {
            s.external_id for s in spaces if not s.quarantine and not s.check_out
        }
        stock = defaultdict(int)
        own_stock = set()
        for unit in self.session.scalars(
            select(Unit)
            .join(Lot)
            .join(Product)
            .join(Location)
            .where(
                Unit.status == "available",
                ~Lot.external_lot_id.startswith("unreported:"),
                ~Lot.external_lot_id.startswith("production:"),
                Unit.effective_expires_at
                > (now if self.tolerant_inventory else demand.deadline),
                ~Unit.order_assignment.has(),
                Unit.lot_id.not_in(select(blocked_lots().c.lot_id)),
                Location.code.in_(eligible_locations),
                ~Unit.market_reservations.any(
                    MarketOfferReservation.released_at.is_(None)
                ),
            )
        ):
            if (
                self.tolerant_inventory
                and unit.lot.product.sku == demand.sku
                and unit.effective_expires_at <= demand.deadline
            ):
                continue
            if not has_official_lineage(unit.lot, allow_external=True):
                continue
            if (
                not catalog[unit.lot.product.sku].storage.cold
                or unit.current_location.is_refrigerated
            ):
                stock[unit.lot.product.sku] += 1
                if unit.lot.produced_by:
                    own_stock.add(unit.lot.product.sku)
        protected = {}
        if self.tolerant_inventory and demand.includes_committed:
            commitments = defaultdict(int)

            def ahead(priority, deadline):
                return priority < demand.priority or (
                    priority == demand.priority and deadline < demand.deadline
                )

            for item in self.session.scalars(
                select(OrderItem)
                .join(Order)
                .where(
                    Order.status.in_(
                        [OrderStatus.RECEIVED, OrderStatus.ACCEPTED, OrderStatus.PAID]
                    ),
                )
            ):
                deadline = item.order.delivery_deadline
                priority = (
                    0
                    if item.order.status in {OrderStatus.ACCEPTED, OrderStatus.PAID}
                    else 1
                )
                if (
                    item.sku != demand.sku
                    and (deadline is None or deadline > now)
                    and ahead(priority, deadline or demand.deadline)
                ):
                    commitments[item.sku] += max(
                        0, item.quantity - len(item.assigned_units)
                    )
            for state in self.session.scalars(
                select(ReplenishmentState).where(
                    ReplenishmentState.period_id == self.period.id,
                    ReplenishmentState.goal.is_not(None),
                )
            ):
                goal = state.goal
                if not goal:
                    continue
                deadline = datetime.fromisoformat(goal["deadline"])
                if (
                    state.sku != demand.sku
                    and deadline > now
                    and ahead(goal.get("priority", 2), deadline)
                ):
                    commitments[state.sku] = max(
                        commitments[state.sku], goal["quantity"]
                    )
            protected = protected_stock(
                catalog, stock, commitments, raw_buffer_batches=0
            )
            for sku, quantity in protected.items():
                stock[sku] = max(0, stock[sku] - quantity)
        else:
            # Legacy callers provide a new demand in addition to existing commitments.
            for item in self.session.scalars(
                select(OrderItem)
                .join(Order)
                .where(
                    Order.status.in_([OrderStatus.ACCEPTED, OrderStatus.PAID]),
                )
            ):
                if demand.includes_committed and item.sku == demand.sku:
                    continue
                stock[item.sku] = max(
                    0,
                    stock[item.sku] - max(0, item.quantity - len(item.assigned_units)),
                )
        incoming = {}
        for run in self.session.scalars(
            select(ProductionRun).where(
                ProductionRun.completed_at.is_(None),
                ProductionRun.available_at <= demand.deadline,
                ProductionRun.available_at.is_not(None),
            )
        ):
            qty, seconds = incoming.get(run.expected_sku, (0, 0))
            incoming[run.expected_sku] = (
                qty + run.expected_quantity,
                max(seconds, (run.available_at - now).total_seconds(), 0),
            )
        pending_supplies = set()
        for state in self.session.scalars(
            select(ReplenishmentState).where(
                ReplenishmentState.period_id == self.period.id,
                ReplenishmentState.pending_operation.is_not(None),
            )
        ):
            op = state.pending_operation or {}
            if op.get("kind") == "supply":
                pending_supplies.add(op["sku"])
        # Pending raw receipts prevent another kit buying the same missing material.
        incoming_role = or_(
            and_(
                MarketOffer.side == MarketOfferSide.BUY,
                MarketOffer.publisher_group_id == self.period.group_id,
            ),
            and_(
                MarketOffer.side == MarketOfferSide.SELL,
                MarketOffer.publisher_group_id != self.period.group_id,
            ),
        )
        # An existing bid is a wait, not supply or capacity. It prevents a second bid.
        active = self.session.scalars(
            select(MarketOffer).where(
                MarketOffer.period_id == self.period.id,
                incoming_role,
                MarketOffer.reconciled_at.is_(None),
                MarketOffer.status.not_in(["cancelled", "expired", "failed"]),
            )
        ).all()
        active = [o for o in active if o.id != replacing_offer_id]
        free_space = sum(
            max(0, s.total_space - s.used_space)
            for s in spaces
            if s.total_space is not None
            and s.used_space is not None
            and not (s.quarantine or s.check_out or s.packaging or s.buffer)
        )
        cold_space = sum(
            max(0, s.total_space - s.used_space)
            for s in spaces
            if s.cold
            and s.total_space is not None
            and s.used_space is not None
            and not (s.quarantine or s.check_out or s.packaging or s.buffer)
        )
        free_space = max(0, free_space - sum(o.quantity for o in active))
        cold_space = max(
            0,
            cold_space
            - sum(
                o.quantity
                for o in active
                if o.sku in catalog and catalog[o.sku].storage.cold
            ),
        )
        skus = set()

        def visit(sku, path=()):
            if sku in path or sku not in catalog:
                raise ValueError("Missing or cyclic catalog recipe")
            skus.add(sku)
            for component in catalog[sku].components:
                visit(component.sku, (*path, sku))

        visit(demand.sku)
        offers = {}
        for sku in sorted(skus):
            offers[sku] = await self.trades.list_offers(
                sku=sku, side="sell", state="open"
            )
        self.session.commit()
        # Estimates for already received stock; these never replace invoices.
        # Known purchases are a reference estimate, not an inferred unit-to-trade
        # price link. Retain the highest observed price when the stock is mixed.
        stock_unit_costs = {}
        if demand.max_outlay is not None:
            for request in self.session.scalars(
                select(ProcurementRequest)
                .where(
                    ProcurementRequest.period_id == self.period.id,
                    ProcurementRequest.status == "acquired",
                )
                .order_by(ProcurementRequest.created_at)
            ):
                acquisition = (request.decision or {}).get("acquisition")
                if acquisition:
                    sku = acquisition["sku"]
                    stock_unit_costs[sku] = max(
                        stock_unit_costs.get(sku, Decimal(0)),
                        Decimal(acquisition["cost"]) / acquisition["quantity"],
                    )
            for sku in own_stock & stock_unit_costs.keys():
                # Mixed stock has no exact purchase-to-unit price evidence.
                # Do not revalue own manufactured units at a cheaper trade price.
                estimated = replacement_cost(catalog, sku)
                if estimated is not None:
                    stock_unit_costs[sku] = max(stock_unit_costs[sku], estimated)
        plan = ProcurementPlanner(
            catalog,
            prices,
            offers,
            stock,
            incoming,
            group_id=self.period.group_id,
            now=now,
            free_space=free_space,
            cold_space=cold_space,
            pending_skus={o.sku for o in active if o.status != "open"}
            | pending_supplies,
            stock_unit_costs=stock_unit_costs,
        ).plan(demand)
        return plan.model_copy(update={"protected_stock": protected})

    async def ensure(
        self, request_key: str, demand: ProcurementDemand
    ) -> ProcurementRequest:
        if not request_key.strip() or len(request_key) > 128:
            raise ValueError(
                "Use a stable nonempty request key of at most 128 characters"
            )
        with procurement_lock(self.session, self.period.id):
            row = self.session.scalar(
                select(ProcurementRequest).where(
                    ProcurementRequest.period_id == self.period.id,
                    ProcurementRequest.request_key == request_key,
                )
            )
            payload = demand.model_dump(mode="json")
            if row is None:
                row = ProcurementRequest(
                    period_id=self.period.id,
                    request_key=request_key,
                    demand=payload,
                    status="waiting",
                )
                self.session.add(row)
                self.session.commit()
            elif (
                ProcurementDemand.model_validate(row.demand).model_dump(mode="json")
                != payload
            ):
                raise ValueError("Request key already belongs to a different demand")
            now = datetime.now(UTC)
            if row.status in {"acquired", "ready"}:
                return row
            if row.next_retry_at and row.next_retry_at > now:
                return row
            cooldown = self.session.scalar(
                select(func.max(ProcurementRequest.next_retry_at)).where(
                    ProcurementRequest.period_id == self.period.id,
                    ProcurementRequest.http_status.in_([401, 403, 429]),
                )
            )
            if cooldown and cooldown > now:
                row.next_retry_at = cooldown
                row.last_error = "Market authentication/rate cooldown"
                self.session.commit()
                return row
            row.http_status = None
            row.next_retry_at = now + timedelta(seconds=60)
            self.session.commit()
            try:
                if row.market_offer_id:
                    return await self._observe(row)
                if (
                    demand.deadline <= now
                    or now < self.period.starts_at
                    or (self.period.ends_at and now >= self.period.ends_at)
                ):
                    row.status = "expired"
                    self.session.commit()
                    return row
                plan = await self.preview(demand)
                if (
                    datetime.now(UTC) + timedelta(seconds=plan.seconds)
                    > demand.deadline
                ):
                    row.status, row.last_error = (
                        "waiting",
                        "Plan no longer fits the delivery deadline",
                    )
                    self.session.commit()
                    return row
                row.decision = plan.model_dump(mode="json")
                row.status, row.last_error = plan.status, plan.reason or None
                self.session.commit()
                action = next(
                    (a for a in plan.actions if a.kind in {"accept", "bid"}), None
                )
                if action is None:
                    return row  # Direct supply/fabrication is integrated in step 13.
                existing = self.session.scalar(
                    select(MarketOffer)
                    .where(
                        MarketOffer.period_id == self.period.id,
                        or_(
                            and_(
                                MarketOffer.side == MarketOfferSide.BUY,
                                MarketOffer.publisher_group_id == self.period.group_id,
                            ),
                            and_(
                                MarketOffer.side == MarketOfferSide.SELL,
                                MarketOffer.publisher_group_id != self.period.group_id,
                            ),
                        ),
                        MarketOffer.sku == action.sku,
                        MarketOffer.reconciled_at.is_(None),
                        MarketOffer.status.not_in(["cancelled", "expired", "failed"]),
                    )
                    .order_by(MarketOffer.created_at)
                    .limit(1)
                )
                if existing:
                    row.market_offer_id = existing.id
                    row.status = "waiting"
                    self.session.commit()
                    return row
                if action.kind == "accept":
                    official = await self.trades.get_offer(action.offer_id)
                    if (
                        official.sku != action.sku
                        or official.quantity != action.quantity
                        or official.quantity * official.unit_price != action.cost
                        or official.status != "open"
                        or official.expires_at is None
                        or official.expires_at
                        <= datetime.now(UTC) + timedelta(seconds=60)
                    ):
                        raise ValueError("Offer changed; replan before accepting")
                    if (
                        datetime.now(UTC) + timedelta(seconds=plan.seconds)
                        > demand.deadline
                    ):
                        raise ValueError("Insufficient time after refreshing the offer")
                    if self.session.scalar(
                        select(ProductionRun.id)
                        .where(
                            ProductionRun.expected_sku == official.sku,
                            ProductionRun.completed_at.is_(None),
                        )
                        .limit(1)
                    ):
                        row.status, row.last_error = (
                            "waiting",
                            "Own production of this SKU is still pending",
                        )
                        self.session.commit()
                        return row
                    local = self.custody._store_offer(official, status="prepared")
                    row.market_offer_id = local.id
                    row.status = "acceptance_pending"
                    self.session.commit()
                    await self.custody.accept_sell(official)
                else:
                    if self.session.scalar(
                        select(ProductionRun.id)
                        .where(
                            ProductionRun.expected_sku == action.sku,
                            ProductionRun.completed_at.is_(None),
                        )
                        .limit(1)
                    ):
                        row.status, row.last_error = (
                            "waiting",
                            "Own production of this SKU is still pending",
                        )
                        self.session.commit()
                        return row
                    count = self.session.scalar(
                        select(func.count())
                        .select_from(MarketOffer)
                        .where(
                            MarketOffer.period_id == self.period.id,
                            MarketOffer.publisher_group_id == self.period.group_id,
                            MarketOffer.status.not_in(
                                ["cancelled", "expired", "closed", "failed"]
                            ),
                        )
                    )
                    if count >= 15:
                        row.status, row.last_error = (
                            "waiting",
                            "Open offer limit reached",
                        )
                        self.session.commit()
                        return row
                    local = MarketOffer(
                        period_id=self.period.id,
                        side=MarketOfferSide.BUY,
                        publisher_group_id=self.period.group_id,
                        sku=action.sku,
                        quantity=action.quantity,
                        unit_price=action.cost / action.quantity,
                        status="creation_uncertain",
                    )
                    self.session.add(local)
                    self.session.flush()
                    row.market_offer_id, row.status = local.id, "creation_pending"
                    self.session.commit()  # Never repeat an unknown POST after restart.
                    request = TradeOfferCreate(
                        side="buy",
                        sku=action.sku,
                        quantity=action.quantity,
                        unitPrice=local.unit_price,
                    )
                    official = await self.trades.create_offer(
                        request, current_unit_price=local.unit_price
                    )
                    self.custody._apply_official_offer(local, official)
                    self.session.commit()
                return await self._observe(row)
            except SQLAlchemyError:
                self.session.rollback()
                raise
            except Exception as exc:
                self.session.rollback()
                if (
                    isinstance(exc, TradesHTTPError)
                    and exc.status_code in {400, 401, 403, 409, 422, 429}
                    and row.market_offer_id
                ):
                    local = self.session.get(MarketOffer, row.market_offer_id)
                    if local.external_offer_id is None:
                        local.status = "failed"
                        row.market_offer_id = None
                row.http_status = getattr(exc, "status_code", None)
                row.last_error = type(exc).__name__
                row.status = "uncertain" if row.market_offer_id else "waiting"
                row.next_retry_at = now + timedelta(
                    seconds=max(60, getattr(exc, "retry_after_seconds", None) or 0)
                )
                self.session.commit()
                return row

    async def _observe(self, row, *, may_cancel=True):
        local = self.session.get(MarketOffer, row.market_offer_id)
        if not local.external_offer_id:
            row.status, row.last_error = (
                "uncertain",
                "Official offer ID unavailable; do not repeat creation",
            )
            self.session.commit()
            return row
        official = await self.trades.get_offer(local.external_offer_id)
        trade = await self.custody.reconcile_offer(official)
        if trade:
            sync = InventorySyncService(self.inventory, self.session)
            if self.tolerant_inventory:
                await sync.synchronize(tolerate_changes=True)
            else:
                await sync.synchronize()
            local.reconciled_at = datetime.now(UTC)
            row.status, row.last_error = "acquired", None
            row.decision = (row.decision or {}) | {
                "acquisition": {
                    "sku": trade.sku,
                    "quantity": trade.quantity,
                    "cost": str(trade.total),
                    "counterparty": trade.counterparty_id,
                    "order_id": trade.external_order_id,
                }
            }
        elif official.status in {"expired", "cancelled", "closed"}:
            local.reconciled_at = datetime.now(UTC)
            row.market_offer_id = None
            row.status = "waiting"
            row.last_error = "Offer ended without a purchase for this group"
        else:
            row.status = "waiting"
            row.last_error = "Open/closing offer is not confirmed stock"
            if (
                may_cancel
                and official.status == "open"
                and official.side == "buy"
                and official.publisher_group == self.period.group_id
            ):
                demand = ProcurementDemand.model_validate(row.demand)
                expired = demand.deadline <= datetime.now(UTC)
                plan = (
                    None
                    if expired
                    else await self.preview(demand, replacing_offer_id=local.id)
                )
                better_route = plan is not None and (
                    plan.status in {"ready", "produce", "direct"}
                    or any(a.kind == "accept" for a in plan.actions)
                )
                if expired or better_route:
                    self.session.commit()
                    await self.trades.cancel_offer(official.id)
                    # A concurrent acceptance may have won. Confirm before replanning.
                    return await self._observe(row, may_cancel=False)
        self.session.commit()
        return row
