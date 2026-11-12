"""E3 replenishment: durable fair turns without sleeping on packaging."""

import asyncio
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.clients.farma_central import FarmaCentralHTTPError
from app.clients.trades import TradesHTTPError
from app.models import (
    Order,
    OrderItem,
    OrderStatus,
    ProcurementRequest,
    ProductionRun,
    ReplenishmentState,
)
from app.schemas.farma_central import FarmaCentralProduct, FarmaCentralSpace
from app.schemas.procurement import ProcurementDemand
from app.services.inventory.sync import InventorySyncService
from app.services.production.locking import ProductionBusyError, production_lock
from app.services.production.orchestration import ProductionError
from app.services.supply.procurement import ScarcityProcurementService
from app.services.supply.requests import (
    ChallengeExpiredError,
    InvalidSupplyRequestError,
    SupplyProductNotFoundError,
    SupplyRequestService,
)

from .cooperative import CooperativeKitProduction, ReplenishmentDeferred, SupplyNeeded
from .replenishment_budget import spending_room
from .replenishment_policy import schedule_key, stock_targets


@contextmanager
def replenishment_lock(session, period_id):
    key = int.from_bytes(
        hashlib.sha256(f"replenishment:{period_id}".encode()).digest()[:8], signed=True
    )
    with session.get_bind().engine.connect() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        connection.commit()
        if not acquired:
            raise ProductionBusyError(
                "Another E3 replenisher is running for this period"
            )
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


class E3ReplenishmentService:
    def __init__(self, client, trades, orders, prices, session, period, *, settings):
        self.client, self.prices, self.session, self.period = (
            client,
            prices,
            session,
            period,
        )
        self.settings = settings
        self.sourcing = ScarcityProcurementService(
            client, trades, orders, prices, session, period, tolerant_inventory=True
        )

    def _producer(self, state, catalog, spaces):
        return CooperativeKitProduction(
            self.client,
            self.session,
            state,
            catalog,
            spaces,
            group_id=self.period.group_id,
        )

    async def poll(self):
        with replenishment_lock(self.session, self.period.id):
            return await self._poll()

    async def _poll(self):
        now = datetime.now(UTC)
        result = {
            "produced": 0,
            "supplied": 0,
            "purchased": 0,
            "ready": 0,
            "deferred": 0,
            "failed": 0,
            "kits": [],
        }
        if now < self.period.starts_at or (
            self.period.ends_at and now >= self.period.ends_at
        ):
            return result
        cooldown = self.session.scalar(
            select(func.max(ReplenishmentState.next_retry_at)).where(
                ReplenishmentState.period_id == self.period.id,
                ReplenishmentState.http_status.in_([401, 403, 429]),
            )
        )
        self.session.commit()
        if cooldown and cooldown > now:
            return result | {"deferred": 1}
        await InventorySyncService(self.client, self.session).synchronize(
            tolerate_changes=True
        )
        catalog_data, spaces_data, prices_data = await asyncio.gather(
            self.client.get_available_products(),
            self.client.get_spaces(),
            self.prices.get_current_prices(use_cache=False),
        )
        catalog = {
            p.sku: p
            for p in TypeAdapter(list[FarmaCentralProduct]).validate_python(
                catalog_data
            )
        }
        spaces = TypeAdapter(list[FarmaCentralSpace]).validate_python(spaces_data)
        prices = {
            p["sku"]: Decimal(str(p["price"]))
            for p in prices_data
            if Decimal(str(p.get("price", 0))).is_finite()
            and Decimal(str(p.get("price", 0))) > 0
        }
        candidates = []
        for kit in catalog.values():
            if not kit.sellable or kit.production.at != "packaging":
                continue
            state = self.session.scalar(
                select(ReplenishmentState).where(
                    ReplenishmentState.period_id == self.period.id,
                    ReplenishmentState.sku == kit.sku,
                )
            )
            if state is None:
                state = ReplenishmentState(period_id=self.period.id, sku=kit.sku)
                self.session.add(state)
                self.session.flush()
            if state.next_retry_at and state.next_retry_at > now:
                continue
            recent = (
                self.session.scalar(
                    select(func.coalesce(func.sum(OrderItem.quantity), 0))
                    .join(Order)
                    .where(
                        OrderItem.sku == kit.sku,
                        Order.status == OrderStatus.DISPATCHED,
                        Order.created_at >= now - timedelta(hours=6),
                    )
                )
                or 0
            )
            state.minimum, state.target = stock_targets(
                kit,
                catalog,
                recent_quantity=recent,
                minimum=self.settings.replenishment_minimum_stock,
                target=self.settings.replenishment_target_stock,
                minimum_lifetime=self.settings.replenishment_minimum_lifetime_seconds,
                group_id=self.period.group_id,
                price=prices.get(kit.sku),
            )
            quantity, deadline, revenue, priority = self._demand(
                kit.sku, prices.get(kit.sku), now
            )
            producer = self._producer(state, catalog, spaces)
            lifetime = self.settings.replenishment_minimum_lifetime_seconds
            if kit.obsolescence:
                lifetime = min(lifetime, kit.obsolescence * 1800)
            if quantity and deadline is not None:
                lifetime = max(0, (deadline - now).total_seconds())
            stock = producer._usable_stock(
                kit.sku,
                lifetime,
            )
            goal_quantity = min(200, quantity) if quantity else state.target
            desired_quantity = goal_quantity
            if (
                not state.goal
                and not state.pending_operation
                and not state.procurement_id
                and (
                    (quantity and stock >= goal_quantity)
                    or (not quantity and stock >= state.minimum)
                )
            ):
                continue
            if state.goal:
                goal_quantity = state.goal["quantity"]
            schedule_deadline = deadline or (
                datetime.fromisoformat(state.goal["deadline"]) if state.goal else None
            )
            candidates.append(
                (
                    schedule_key(
                        priority,
                        schedule_deadline,
                        state.last_attempt_at,
                        goal_quantity - stock,
                        kit.sku,
                    ),
                    state,
                    desired_quantity,
                    deadline,
                    revenue,
                    priority,
                )
            )
        self.session.commit()
        for _, state, quantity, deadline, revenue, priority in sorted(
            candidates, key=lambda c: c[0]
        )[:3]:
            state.last_attempt_at = datetime.now(UTC)
            state.next_retry_at = state.last_attempt_at + timedelta(seconds=60)
            state.last_error = None
            state.http_status = None
            self.session.commit()
            producer = self._producer(state, catalog, spaces)
            previous_operation_key = (state.pending_operation or {}).get("key")
            try:
                if await self._resume_operation(state, producer):
                    raise ReplenishmentDeferred(
                        "Operation pending confirmed receipt/recovery"
                    )
                if state.procurement_id:
                    request = self.session.get(ProcurementRequest, state.procurement_id)
                    request = await self.sourcing.ensure(
                        request.request_key,
                        ProcurementDemand.model_validate(request.demand),
                    )
                    if request.status == "acquired":
                        state.estimated_spend += Decimal(
                            request.decision["acquisition"]["cost"]
                        )
                        state.procurement_id = None
                        self.session.commit()
                        result["purchased"] += 1
                    elif request.status in {"ready", "direct", "produce", "expired"}:
                        if request.status in {"direct", "produce"}:
                            request.status = "superseded"
                        if request.status == "expired":
                            state.goal = None
                        state.procurement_id = None
                        self.session.commit()
                    else:
                        if request.http_status in {401, 403, 429}:
                            raise TradesHTTPError(
                                request.http_status,
                                retry_after_seconds=max(
                                    60,
                                    (
                                        request.next_retry_at - datetime.now(UTC)
                                    ).total_seconds(),
                                ),
                            )
                        raise ReplenishmentDeferred(
                            request.last_error or "Market sourcing pending"
                        )
                if (
                    state.goal
                    and not state.procurement_id
                    and (
                        priority < state.goal.get("priority", 2)
                        or (
                            deadline is not None
                            and deadline
                            < datetime.fromisoformat(state.goal["deadline"])
                        )
                    )
                ):
                    # A new customer deadline supersedes a background buffer goal.
                    state.goal = state.goal | {
                        "quantity": quantity,
                        "deadline": (
                            deadline or datetime.fromisoformat(state.goal["deadline"])
                        ).isoformat(),
                        "revenue": str(revenue),
                        "priority": priority,
                    }
                    self.session.commit()
                if (
                    state.goal
                    and not state.procurement_id
                    and state.goal.get("priority", 2) == priority == 2
                    and state.goal["quantity"] != quantity
                ):
                    state.goal = state.goal | {
                        "quantity": quantity,
                        "revenue": str(prices[state.sku] * quantity),
                    }
                    self.session.commit()
                if not state.goal:
                    if revenue is None or revenue <= 0:
                        raise ReplenishmentDeferred(
                            "No current sale value to justify procurement"
                        )
                    shelf_hours = catalog[state.sku].obsolescence
                    deadline = deadline or datetime.now(UTC) + timedelta(
                        hours=min(2, shelf_hours / 2) if shelf_hours else 2
                    )
                    if self.period.ends_at:
                        deadline = min(deadline, self.period.ends_at)
                    if priority == 2:
                        revenue = prices[state.sku] * quantity
                    state.goal = {
                        "sku": state.sku,
                        "quantity": quantity,
                        "deadline": deadline.isoformat(),
                        "revenue": str(revenue),
                        "priority": priority,
                        "spend_started": str(state.estimated_spend),
                    }
                    self.session.commit()
                goal = state.goal
                if datetime.fromisoformat(goal["deadline"]) <= datetime.now(UTC):
                    state.goal = None
                    self.session.commit()
                    raise ReplenishmentDeferred(
                        "Previous replenishment goal expired; recompute demand"
                    )
                # Put actual receipts/finished outputs into sellable storage first.
                with production_lock(self.session):
                    pending_run = self.session.scalar(
                        select(ProductionRun.id)
                        .where(ProductionRun.completed_at.is_(None))
                        .limit(1)
                    )
                    await producer.put_away(
                        include_packaging=pending_run is None
                        and not producer._unknown_production()
                    )
                if producer._usable_stock(state.sku, 0) >= goal["quantity"]:
                    state.goal = None
                    self.session.commit()
                    result["ready"] += 1
                    continue
                room, budget_details = spending_room(
                    self.session, self.period, self.settings, catalog, prices
                )
                spent = state.estimated_spend - Decimal(goal.get("spend_started", "0"))
                # Bound one replenishment to the value of one useful target
                # basket, and all replenishments to the shared period envelope.
                goal_limit = max(
                    Decimal(goal["revenue"]),
                    prices.get(state.sku, Decimal(0))
                    * max(state.target, goal["quantity"]),
                )
                max_outlay = spent + min(room, max(Decimal(0), goal_limit - spent))
                demand = ProcurementDemand(
                    sku=state.sku,
                    quantity=goal["quantity"],
                    deadline=goal["deadline"],
                    expected_revenue=goal["revenue"],
                    other_costs=spent,
                    max_outlay=max_outlay,
                    minimum_margin=0
                    if goal.get("priority", priority) == 0
                    else Decimal("0.10"),
                    includes_committed=True,
                    priority=goal.get("priority", priority),
                )
                # Reuse this cycle's catalog/prices and synchronized unit ledger;
                # movements require only a fresh capacity reading. Market execution
                # still performs its own preflight before any mutation.
                current_spaces = TypeAdapter(list[FarmaCentralSpace]).validate_python(
                    await self.client.get_spaces()
                )
                plan = await self.sourcing.preview(
                    demand, snapshot=(catalog, current_spaces, prices)
                )
                state.goal = goal | {
                    "budget": budget_details,
                    "max_outlay": str(max_outlay),
                    "planned_outlay": str(plan.cost),
                    "estimated_consumption": str(plan.consumed_cost)
                    if plan.consumed_cost is not None
                    else None,
                }
                self.session.commit()
                market = next(
                    (a for a in plan.actions if a.kind in {"accept", "bid"}), None
                )
                if market:
                    request = ProcurementRequest(
                        period_id=self.period.id,
                        request_key=f"replenish:{state.id}:{uuid4()}",
                        demand=demand.model_dump(mode="json"),
                        status="waiting",
                    )
                    self.session.add(request)
                    self.session.flush()
                    state.procurement_id = request.id
                    self.session.commit()
                    request = await self.sourcing.ensure(request.request_key, demand)
                    if request.status == "acquired":
                        state.estimated_spend += Decimal(
                            request.decision["acquisition"]["cost"]
                        )
                        state.procurement_id = None
                        self.session.commit()
                        result["purchased"] += 1
                    if request.http_status in {401, 403, 429}:
                        raise TradesHTTPError(
                            request.http_status,
                            retry_after_seconds=max(
                                60,
                                (
                                    request.next_retry_at - datetime.now(UTC)
                                ).total_seconds(),
                            ),
                        )
                    raise ReplenishmentDeferred(
                        "Sourcing turn complete; re-evaluate actual stock next cycle"
                    )
                action = next((a for a in plan.actions if a.kind == "direct"), None)
                if action:
                    state.pending_operation = {
                        "kind": "supply",
                        "key": str(uuid4()),
                        "sku": action.sku,
                        "quantity": action.quantity,
                        "required": len(producer._available_units(action.sku))
                        + action.useful_quantity,
                        "cost": str(action.cost),
                        "status": "in_flight",
                    }
                    self.session.commit()
                    response = await SupplyRequestService(
                        self.client, self.session
                    ).request(sku=action.sku, quantity=action.quantity)
                    state.pending_operation = state.pending_operation | {
                        "status": "confirmed",
                        "available_at": response.available_at.isoformat(),
                    }
                    self.session.commit()
                    result["supplied"] += 1
                    raise ReplenishmentDeferred(
                        "Supply confirmed; revisit after its availability time"
                    )
                if plan.status == "waiting":
                    raise ReplenishmentDeferred(
                        plan.reason or "Waiting for a feasible replenishment route"
                    )
                producer.protected_stock = plan.protected_stock
                await producer.advance(state.sku, goal["quantity"])
            except ReplenishmentDeferred as exc:
                state.last_error = str(exc)
                result["deferred"] += 1
                if (
                    state.pending_operation
                    and state.pending_operation["kind"] == "produce"
                    and state.pending_operation["status"] == "confirmed"
                    and state.pending_operation["key"] != previous_operation_key
                ):
                    result["produced"] += 1
                self.session.commit()
            except ProductionBusyError:
                state.last_error = "Packaging busy; retry on the next turn"
                result["deferred"] += 1
                self.session.commit()
            except SQLAlchemyError:
                self.session.rollback()
                raise
            except Exception as exc:
                self.session.rollback()
                op = state.pending_operation
                if op and op.get("status") == "in_flight":
                    definite = isinstance(
                        exc,
                        (
                            ProductionError,
                            SupplyNeeded,
                            InvalidSupplyRequestError,
                            SupplyProductNotFoundError,
                            ChallengeExpiredError,
                        ),
                    ) or (
                        isinstance(exc, FarmaCentralHTTPError)
                        and exc.status_code in {400, 401, 403, 409, 422, 429}
                    )
                    state.pending_operation = (
                        None if definite else op | {"status": "uncertain"}
                    )
                state.http_status = getattr(exc, "status_code", None)
                state.last_error = type(exc).__name__
                state.next_retry_at = datetime.now(UTC) + timedelta(
                    seconds=max(120, getattr(exc, "retry_after_seconds", None) or 0)
                )
                self.session.commit()
                result["failed"] += 1
                if getattr(exc, "status_code", None) in {401, 403, 429}:
                    break
            finally:
                result["kits"].append(
                    {
                        "sku": state.sku,
                        "minimum": state.minimum,
                        "target": state.target,
                        "priority": priority,
                        "planned_outlay": (state.goal or {}).get("planned_outlay"),
                        "estimated_consumption": (state.goal or {}).get(
                            "estimated_consumption"
                        ),
                        "spending_room": (state.goal or {})
                        .get("budget", {})
                        .get("remaining"),
                        "last_error": state.last_error,
                        "next_retry_at": state.next_retry_at.isoformat()
                        if state.next_retry_at
                        else None,
                    }
                )
            if (
                result["produced"]
                or result["supplied"]
                or result["purchased"]
                or (
                    state.pending_operation
                    and state.pending_operation.get("status") == "uncertain"
                )
            ):
                break
        return result

    async def _resume_operation(self, state, producer):
        op = state.pending_operation
        if not op:
            return False
        if op["kind"] == "produce":
            run = self.session.scalar(
                select(ProductionRun).where(
                    ProductionRun.operation_key == UUID(op["key"])
                )
            )
            if (
                run is not None
                and run.completed_at is not None
                and run.output_lot_id is not None
            ):
                state.estimated_spend += Decimal(op["cost"])
                state.pending_operation = None
                self.session.commit()
                return False
            if run is not None:
                state.pending_operation = op | {
                    "status": "confirmed",
                    "available_at": run.available_at.isoformat()
                    if run.available_at
                    else None,
                }
                self.session.commit()
                return True
        elif (
            op["status"] == "confirmed"
            and datetime.fromisoformat(op["available_at"]) <= datetime.now(UTC)
            and len(producer._available_units(op["sku"])) >= op["required"]
        ):
            state.estimated_spend += Decimal(op["cost"])
            state.pending_operation = None
            self.session.commit()
            return False
        if op["status"] == "in_flight":
            state.pending_operation = op | {"status": "uncertain"}
            self.session.commit()
        return True

    def _demand(self, sku, price, now):
        items = self.session.scalars(
            select(OrderItem)
            .join(Order)
            .where(
                OrderItem.sku == sku,
                Order.status.in_(
                    [OrderStatus.ACCEPTED, OrderStatus.PAID, OrderStatus.RECEIVED]
                ),
            )
        ).all()
        work = [
            (item, max(0, item.quantity - len(item.assigned_units)))
            for item in items
            if item.order.delivery_deadline is None
            or item.order.delivery_deadline > now
        ]
        accepted = [
            (item, q)
            for item, q in work
            if q and item.order.status in {OrderStatus.ACCEPTED, OrderStatus.PAID}
        ]
        selected = accepted or [(item, q) for item, q in work if q]
        if selected:
            selected.sort(
                key=lambda pair: (
                    pair[0].order.delivery_deadline or datetime.max.replace(tzinfo=UTC)
                )
            )
            # Use the earliest outstanding order's fixed price and deadline.
            item, quantity = selected[0]
            quantity = min(quantity, 200)
            return (
                quantity,
                item.order.delivery_deadline,
                item.unit_price * quantity,
                0 if accepted else 1,
            )
        return (
            0,
            None,
            price * self.settings.replenishment_target_stock if price else None,
            2,
        )
