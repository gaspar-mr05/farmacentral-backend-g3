"""E3 replenishment: durable fair turns without sleeping on packaging."""

import asyncio
import hashlib
import logging
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.clients.farma_central import FarmaCentralHTTPError
from app.clients.trades import TradesHTTPError
from app.models import (
    MarketOffer,
    MarketRole,
    MarketTrade,
    Order,
    OrderItem,
    OrderSource,
    OrderStatus,
    ProcurementRequest,
    ProductionRun,
    ReplenishmentState,
)
from app.schemas.farma_central import FarmaCentralProduct, FarmaCentralSpace
from app.schemas.procurement import ProcurementDemand
from app.services.inventory.sync import InventorySyncService
from app.services.production.locking import ProductionBusyError, production_lock
from app.services.production.orchestration import (
    ProductionError,
    ProductionInputsUnavailableError,
)
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
from .supply_receipts import supply_receipt_baseline, supply_resume_basis

logger = logging.getLogger(__name__)


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
            inventory_fresh=True,
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
        phase_started = perf_counter()
        observation = await InventorySyncService(
            self.client, self.session
        ).observe_recent(
            path=self.settings.effective_inventory_observation_file,
            max_age_seconds=self._inventory_max_age(now),
            tolerate_changes=True,
            # Large stock groups are truncated conservatively by the collector.
            # Mutations still preflight, and due/uncertain operations disable reuse.
            allow_incomplete=True,
        )
        inventory_started_at = observation.started_at
        synchronized = observation.result
        logger.info(
            "Replenishment timing: stage=inventory elapsed_seconds=%.2f reused=%s",
            perf_counter() - phase_started,
            observation.reused,
        )
        phase_started = perf_counter()
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
                        Order.external_order_id.is_not(None),
                        Order.source.in_(
                            [
                                OrderSource.SFTP,
                                OrderSource.PUBSUB,
                                OrderSource.MESSAGING,
                            ]
                        ),
                        Order.status.not_in(
                            [OrderStatus.CANCELLED, OrderStatus.EXPIRED]
                        ),
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
                kit.sku,
                prices.get(kit.sku),
                now,
                preferred_order_id=(state.goal or {}).get("order_id"),
                preferred_order_ids=(state.goal or {}).get("order_ids"),
                preferred_selected_at=(state.goal or {}).get("selected_at"),
            )
            if (
                state.goal
                and state.goal.get("buffer_recovery")
                and priority == 1
                and state.goal.get("buffer_recovery_for")
                == {
                    "quantity": quantity,
                    "deadline": deadline.isoformat() if deadline else None,
                    "revenue": str(revenue),
                }
                and datetime.fromisoformat(state.goal["deadline"]) > now
            ):
                quantity = state.goal["quantity"]
                deadline = datetime.fromisoformat(state.goal["deadline"])
                revenue = Decimal(state.goal["revenue"])
                priority = state.goal["priority"]
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
                        resume_ready=self._operation_ready(state, producer, now),
                    ),
                    state,
                    desired_quantity,
                    deadline,
                    revenue,
                    priority,
                )
            )
        self.session.commit()
        # A complete public offer book and the inventory/catalog snapshot remain
        # valid until this cycle performs a market or packaging mutation. Direct
        # supplies do not change either, so later recipes can reuse the quotes.
        offer_cache = {}
        logger.info(
            "Replenishment timing: stage=planning elapsed_seconds=%.2f candidates=%s",
            perf_counter() - phase_started,
            len(candidates),
        )
        for _, state, quantity, deadline, revenue, priority in sorted(
            candidates, key=lambda c: c[0]
        )[:3]:
            turn_started = perf_counter()
            if deadline is not None and self.period.ends_at:
                deadline = min(deadline, self.period.ends_at)
            state.last_attempt_at = datetime.now(UTC)
            state.next_retry_at = state.last_attempt_at + timedelta(seconds=60)
            state.last_error = None
            state.http_status = None
            self.session.commit()
            producer = self._producer(state, catalog, spaces)
            previous_operation_key = (state.pending_operation or {}).get("key")
            using_pending_custody = False
            try:
                if await self._resume_operation(state, producer):
                    raise ReplenishmentDeferred(
                        "Operation pending confirmed receipt/recovery"
                    )
                if state.procurement_id:
                    request = self.session.get(ProcurementRequest, state.procurement_id)
                    saved_demand = ProcurementDemand.model_validate(request.demand)
                    recovery_goal = None
                    retarget = saved_demand.priority != priority or (
                        priority < 2
                        and (
                            saved_demand.quantity != quantity
                            or saved_demand.expected_revenue != revenue
                            or deadline != saved_demand.deadline
                            or saved_demand.maximum_loss_fraction
                            != self.settings.replenishment_order_loss_limit
                        )
                    )
                    if (
                        not retarget
                        and priority == 1
                        and state.goal
                        and self._request_has_final_kit_bid(request, state.sku)
                        and prices.get(state.sku, Decimal(0)) > 0
                    ):
                        replacement_room, _ = spending_room(
                            self.session,
                            self.period,
                            self.settings,
                            catalog,
                            prices,
                            replacing_offer_id=request.market_offer_id,
                        )
                        candidate = self._buffer_recovery_goal(
                            state,
                            state.goal,
                            catalog[state.sku],
                            prices[state.sku],
                        )
                        recovery_plan = await self.sourcing.preview(
                            self._procurement_demand(
                                state, candidate, replacement_room, prices
                            ),
                            replacing_offer_id=request.market_offer_id,
                            snapshot=(catalog, spaces, prices),
                            offer_cache=offer_cache,
                        )
                        if not self._needs_buffer_recovery(recovery_plan, state.sku):
                            recovery_goal = candidate
                            retarget = True
                    request = await self.sourcing.ensure(
                        request.request_key,
                        saved_demand,
                        retire=retarget,
                        snapshot=(catalog, spaces, prices),
                        inventory_observation=(inventory_started_at, synchronized),
                        offer_cache=offer_cache,
                    )
                    if request.status == "acquired":
                        state.estimated_spend += Decimal(
                            request.decision["acquisition"]["cost"]
                        )
                        state.procurement_id = None
                        self.session.commit()
                        result["purchased"] += 1
                    elif request.status in {
                        "ready",
                        "direct",
                        "produce",
                        "expired",
                        "superseded",
                    }:
                        if request.status in {"direct", "produce"}:
                            request.status = "superseded"
                        if request.status == "expired":
                            state.goal = None
                        state.procurement_id = None
                        if recovery_goal is not None:
                            state.goal = recovery_goal
                            quantity = recovery_goal["quantity"]
                            deadline = datetime.fromisoformat(recovery_goal["deadline"])
                            revenue = Decimal(recovery_goal["revenue"])
                            priority = recovery_goal["priority"]
                        self.session.commit()
                    elif (
                        request.status == "custody_pending"
                        and request.http_status is None
                        and self._account_confirmed_custody_purchase(state, request)
                    ):
                        # Physical sourcing can continue. The independent trade
                        # reconciler still owns the unresolved unit association.
                        result["purchased"] += 1
                    elif (
                        request.status == "custody_pending"
                        and request.http_status is None
                        and await self._can_use_verified_stock(
                            state,
                            (catalog, spaces, prices),
                            offer_cache,
                            quantity=quantity,
                            deadline=deadline,
                            revenue=revenue,
                            priority=priority,
                        )
                    ):
                        # Preserve both the procurement link and its committed
                        # cost. Custody reconciliation continues independently;
                        # manufacturing uses only the verified stock ledger.
                        using_pending_custody = True
                        logger.info(
                            "Replenishment using verified stock: kit=%s "
                            "procurement_id=%s custody_pending=True",
                            state.sku,
                            request.id,
                        )
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
                    and priority == 2
                    and state.goal.get("priority", 2) < 2
                ):
                    state.goal = None
                    self.session.commit()
                if (
                    state.goal
                    and not state.procurement_id
                    and (
                        priority != state.goal.get("priority", 2)
                        or (priority < 2 and state.goal["quantity"] != quantity)
                        or (priority < 2 and Decimal(state.goal["revenue"]) != revenue)
                        or (
                            deadline is not None
                            and deadline
                            != datetime.fromisoformat(state.goal["deadline"])
                        )
                    )
                ):
                    # Follow the current OC, including after the previous one ended.
                    updated_goal = state.goal | {
                        "quantity": quantity,
                        "deadline": (
                            deadline or datetime.fromisoformat(state.goal["deadline"])
                        ).isoformat(),
                        "revenue": str(revenue),
                        "priority": priority,
                    }
                    updated_goal.pop("buffer_recovery", None)
                    updated_goal.pop("buffer_recovery_for", None)
                    state.goal = updated_goal
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
                    state.goal = self._new_goal(
                        state, catalog, prices, quantity, deadline, revenue, priority
                    )
                    self.session.commit()
                goal = state.goal
                if datetime.fromisoformat(goal["deadline"]) <= datetime.now(UTC):
                    state.goal = None
                    self.session.commit()
                    raise ReplenishmentDeferred(
                        "Previous replenishment goal expired; recompute demand"
                    )
                # Put finished kits into sellable storage; recipe inputs can
                # move directly from reception to packaging when staged.
                with production_lock(self.session):
                    pending_run = self.session.scalar(
                        select(ProductionRun.id)
                        .where(ProductionRun.completed_at.is_(None))
                        .limit(1)
                    )
                    await producer.put_away(
                        include_packaging=pending_run is None
                        and not producer._unknown_production(),
                        receiving_skus=(state.sku,),
                    )
                if producer._usable_stock(state.sku, 0) >= goal["quantity"]:
                    state.goal = None
                    self.session.commit()
                    result["ready"] += 1
                    continue
                room, budget_details = spending_room(
                    self.session, self.period, self.settings, catalog, prices
                )
                demand = self._procurement_demand(state, goal, room, prices)
                # Reuse this cycle's catalog/prices and synchronized unit ledger;
                # movements require only a fresh capacity reading. Market execution
                # still performs its own preflight before any mutation.
                current_spaces = TypeAdapter(list[FarmaCentralSpace]).validate_python(
                    await self.client.get_spaces()
                )
                # Compare alternatives for this one decision against the same
                # quotes, without multiplying remote reads. Execution is fresh.
                plan = await self.sourcing.preview(
                    demand,
                    snapshot=(catalog, current_spaces, prices),
                    offer_cache=offer_cache,
                )
                execution_quantity = goal["quantity"]
                partial = False
                producer.protected_stock = plan.protected_stock
                ready_quantity = producer.ready_final_quantity(execution_quantity)
                waiting_orders = self._order_demands(state.sku, datetime.now(UTC))
                if (
                    ready_quantity is not None
                    and waiting_orders
                    and waiting_orders[0][1] <= ready_quantity
                ):
                    partial_demand = demand.model_copy(
                        update={
                            "quantity": ready_quantity,
                            "expected_revenue": demand.expected_revenue
                            * ready_quantity
                            / demand.quantity,
                        }
                    )
                    partial_plan = await self.sourcing.preview(
                        partial_demand,
                        snapshot=(catalog, current_spaces, prices),
                        offer_cache=offer_cache,
                    )
                    if partial_plan.status == "produce" and not partial_plan.actions:
                        execution_quantity = ready_quantity
                        plan = partial_plan
                        partial = True
                        logger.info(
                            "Partial kit selected: sku=%s quantity=%s goal=%s",
                            state.sku,
                            ready_quantity,
                            goal["quantity"],
                        )
                if using_pending_custody and (
                    not self._can_advance_with_pending_custody(plan)
                ):
                    raise ReplenishmentDeferred(
                        "Verified stock changed; keep custody and budget pending"
                    )
                if self._needs_buffer_recovery(
                    plan, state.sku
                ) and await self._select_later_order(
                    state, room, (catalog, current_spaces, prices), offer_cache
                ):
                    raise ReplenishmentDeferred(
                        "Selected a viable later order; revalidate next cycle"
                    )
                if (
                    goal.get("priority") == 1
                    and prices.get(state.sku, Decimal(0)) > 0
                    and self._needs_buffer_recovery(plan, state.sku)
                ):
                    recovery_goal = self._buffer_recovery_goal(
                        state, goal, catalog[state.sku], prices[state.sku]
                    )
                    recovery_demand = self._procurement_demand(
                        state, recovery_goal, room, prices
                    )
                    recovery_plan = await self.sourcing.preview(
                        recovery_demand,
                        snapshot=(catalog, current_spaces, prices),
                        offer_cache=offer_cache,
                    )
                    if not self._needs_buffer_recovery(recovery_plan, state.sku):
                        goal = recovery_goal
                        demand = recovery_demand
                        plan = recovery_plan
                        state.goal = goal
                        self.session.commit()
                        logger.info(
                            "Replenishment switched to buffer recovery: "
                            "sku=%s quantity=%s deadline=%s",
                            state.sku,
                            goal["quantity"],
                            goal["deadline"],
                        )
                state.goal = goal | {
                    "budget": budget_details,
                    "max_outlay": str(demand.max_outlay),
                    "planned_outlay": str(plan.cost),
                    "estimated_consumption": str(plan.consumed_cost)
                    if plan.consumed_cost is not None
                    else None,
                }
                self.session.commit()
                producer.protected_stock = plan.protected_stock
                producer.sourcing_skus = {a.sku for a in plan.actions}
                action = next((a for a in plan.actions if a.sku == state.sku), None)
                if action is None:
                    if plan.status == "waiting" and not plan.actions:
                        raise ReplenishmentDeferred(
                            plan.reason or "Waiting for a feasible replenishment route"
                        )
                    # Advance the recipe before buying ingredients for later stages.
                    # This may start one manufacture, or identify its first shortage.
                    try:
                        if partial:
                            await producer.advance(
                                state.sku, execution_quantity, ready_only=True
                            )
                        else:
                            await producer.advance(state.sku, goal["quantity"])
                    except SupplyNeeded as exc:
                        action = next(
                            (a for a in plan.actions if a.sku == exc.sku), None
                        )
                        if action is None:
                            # Stock can change between preview and staging. Re-read
                            # and replan once rather than repeating an obsolete plan.
                            await InventorySyncService(
                                self.client, self.session
                            ).synchronize(tolerate_changes=True)
                            room, budget_details = spending_room(
                                self.session,
                                self.period,
                                self.settings,
                                catalog,
                                prices,
                            )
                            demand = self._procurement_demand(state, goal, room, prices)
                            current_spaces = TypeAdapter(
                                list[FarmaCentralSpace]
                            ).validate_python(await self.client.get_spaces())
                            plan = await self.sourcing.preview(
                                demand,
                                snapshot=(catalog, current_spaces, prices),
                                offer_cache=offer_cache,
                            )
                            state.goal = goal | {
                                "budget": budget_details,
                                "planned_outlay": str(plan.cost),
                                "estimated_consumption": str(plan.consumed_cost)
                                if plan.consumed_cost is not None
                                else None,
                            }
                            self.session.commit()
                            if (
                                using_pending_custody
                                and not self._can_advance_with_pending_custody(plan)
                            ):
                                raise ReplenishmentDeferred(
                                    "Verified stock changed; "
                                    "keep custody and budget pending"
                                ) from exc
                            action = next(
                                (a for a in plan.actions if a.sku == exc.sku), None
                            )
                            if action is None:
                                raise ReplenishmentDeferred(
                                    f"Stock changed for {exc.sku}; refreshed plan: "
                                    f"{plan.reason or plan.status}"
                                ) from exc
                    else:
                        continue
                logger.info(
                    "Replenishment input selected: kit=%s sku=%s kind=%s quantity=%s",
                    state.sku,
                    action.sku,
                    action.kind,
                    action.quantity,
                )
                market = action if action.kind in {"accept", "bid"} else None
                if market:
                    request = ProcurementRequest(
                        period_id=self.period.id,
                        request_key=f"replenish:{state.id}:{uuid4()}",
                        demand=demand.model_dump(mode="json"),
                        decision={"next_action_sku": market.sku},
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
                if action.kind == "direct":
                    # Receipt confirmation compares the whole physical stock,
                    # including stock protected for other orders.
                    producer.protected_stock = {}
                    state.pending_operation = {
                        "kind": "supply",
                        "key": str(uuid4()),
                        "sku": action.sku,
                        "quantity": action.quantity,
                        "needed": action.useful_quantity,
                        "cost": str(action.cost),
                        "status": "in_flight",
                        **supply_receipt_baseline(
                            self.session,
                            self.period,
                            action.sku,
                            inventory_complete=action.sku in synchronized.complete_skus,
                        ),
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
                    logger.info(
                        "Replenishment production confirmed: kit=%s sku=%s quantity=%s",
                        state.sku,
                        state.pending_operation["sku"],
                        state.pending_operation["quantity"],
                    )
                self.session.commit()
            except ProductionBusyError:
                state.last_error = "Packaging busy; retry on the next turn"
                result["deferred"] += 1
                self.session.commit()
            except ProductionInputsUnavailableError as exc:
                self.session.rollback()
                # This exception is raised before challenge/production requests.
                state.pending_operation = None
                state.last_error = str(exc)
                state.next_retry_at = datetime.now(UTC) + timedelta(seconds=60)
                self.session.commit()
                result["deferred"] += 1
                logger.warning(
                    "Replenishment inputs unavailable: kit=%s missing=%s",
                    state.sku,
                    exc.missing_by_sku,
                )
            except SQLAlchemyError:
                self.session.rollback()
                raise
            except Exception as exc:
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                logger.error(
                    "Replenishment failed: kit=%s error_type=%s location=%s:%s:%s",
                    state.sku,
                    type(exc).__name__,
                    Path(frame.filename).name,
                    frame.name,
                    frame.lineno,
                )
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
                logger.info(
                    "Replenishment timing: stage=kit sku=%s elapsed_seconds=%.2f",
                    state.sku,
                    perf_counter() - turn_started,
                )
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
                or result["purchased"]
                or (
                    state.pending_operation
                    and state.pending_operation.get("status") == "uncertain"
                )
            ):
                break
        return result

    def _inventory_max_age(self, now: datetime) -> float:
        """Never delay recovery of an operation that can already be available."""
        due_production = self.session.scalar(
            select(ProductionRun.id)
            .where(
                ProductionRun.completed_at.is_(None),
                or_(
                    ProductionRun.available_at.is_(None),
                    ProductionRun.available_at <= now,
                ),
            )
            .limit(1)
        )
        if due_production is not None:
            return 0

        pending_operations = self.session.scalars(
            select(ReplenishmentState.pending_operation).where(
                ReplenishmentState.period_id == self.period.id,
                ReplenishmentState.pending_operation.is_not(None),
            )
        )
        for operation in pending_operations:
            operation = operation or {}
            status = operation.get("status")
            if status in {"in_flight", "uncertain"}:
                return 0
            available_at = operation.get("available_at")
            if status == "confirmed" and (
                not available_at or datetime.fromisoformat(available_at) <= now
            ):
                return 0
        return self.settings.replenishment_inventory_max_age_seconds

    def _new_goal(self, state, catalog, prices, quantity, deadline, revenue, priority):
        if revenue is None or revenue <= 0:
            raise ReplenishmentDeferred("No current sale value to justify procurement")
        shelf_hours = catalog[state.sku].obsolescence
        deadline = deadline or datetime.now(UTC) + max(
            timedelta(hours=2),
            timedelta(seconds=self.settings.replenishment_minimum_lifetime_seconds),
            timedelta(hours=shelf_hours / 2) if shelf_hours else timedelta(0),
        )
        if self.period.ends_at:
            deadline = min(deadline, self.period.ends_at)
        return {
            "sku": state.sku,
            "quantity": quantity,
            "deadline": deadline.isoformat(),
            "revenue": str(prices[state.sku] * quantity if priority == 2 else revenue),
            "priority": priority,
            "spend_started": str(state.estimated_spend),
        }

    def _account_confirmed_custody_purchase(self, state, request):
        trade = self.session.scalar(
            select(MarketTrade)
            .join(
                MarketOffer,
                (MarketOffer.external_offer_id == MarketTrade.external_offer_id)
                & (MarketOffer.period_id == MarketTrade.period_id),
            )
            .where(
                MarketOffer.id == request.market_offer_id,
                MarketOffer.status == "closed",
                MarketTrade.period_id == self.period.id,
                MarketTrade.role == MarketRole.BUYER,
            )
        )
        if trade is None:
            return False
        decision = request.decision or {}
        accounted = self.session.scalar(
            select(ProcurementRequest.id)
            .where(
                ProcurementRequest.period_id == self.period.id,
                ProcurementRequest.market_offer_id == request.market_offer_id,
                ProcurementRequest.decision["replenishment_accounted"]
                .as_boolean()
                .is_(True),
            )
            .limit(1)
        )
        if accounted is None:
            state.estimated_spend += trade.total
        request.decision = decision | {"replenishment_accounted": True}
        state.procurement_id = None
        self.session.commit()
        logger.info(
            "Closed purchase accounted; custody recovery retained: kit=%s trade=%s",
            state.sku,
            trade.id,
        )
        return True

    async def _can_use_verified_stock(
        self, state, snapshot, offer_cache, *, quantity, deadline, revenue, priority
    ):
        catalog, _, prices = snapshot
        goal = state.goal
        # An unresolved trade must not pin manufacturing to an expired order,
        # nor prevent a new goal after the previous kit was completed.
        if (
            not goal
            or datetime.fromisoformat(goal["deadline"]) <= datetime.now(UTC)
            or goal.get("priority", 2) != priority
            or (
                priority < 2
                and (
                    goal["quantity"] != quantity
                    or Decimal(goal["revenue"]) != revenue
                    or (
                        deadline
                        and datetime.fromisoformat(goal["deadline"]) != deadline
                    )
                )
            )
        ):
            goal = self._new_goal(
                state, catalog, prices, quantity, deadline, revenue, priority
            )
        room, _ = spending_room(
            self.session, self.period, self.settings, catalog, prices
        )
        plan = await self.sourcing.preview(
            self._procurement_demand(state, goal, room, prices),
            snapshot=snapshot,
            offer_cache=offer_cache,
        )
        if not self._can_advance_with_pending_custody(plan):
            # Unresolved custody must not prevent a whole customer order that
            # can be finished using only verified components already on hand.
            producer = self._producer(state, catalog, snapshot[1])
            producer.protected_stock = plan.protected_stock
            ready = producer.ready_final_quantity(goal["quantity"])
            orders = self._order_demands(state.sku, datetime.now(UTC))
            if ready is None or not orders or orders[0][1] > ready:
                return False
            partial_demand = self._procurement_demand(state, goal, room, prices)
            partial_plan = await self.sourcing.preview(
                partial_demand.model_copy(
                    update={
                        "quantity": ready,
                        "expected_revenue": partial_demand.expected_revenue
                        * ready
                        / partial_demand.quantity,
                    }
                ),
                snapshot=snapshot,
                offer_cache=offer_cache,
            )
            if partial_plan.status != "produce" or partial_plan.actions:
                return False
        state.goal = goal
        self.session.commit()
        return True

    @staticmethod
    def _can_advance_with_pending_custody(plan):
        # A closed purchase awaiting unit association must not block requesting
        # other directly supplied ingredients. Keep its procurement link and
        # cost reserved; a second market acquisition needs that same link.
        return plan.status in {"ready", "produce"} and all(
            action.kind == "direct" for action in plan.actions
        )

    def _operation_ready(self, state, producer, now):
        op = state.pending_operation or {}
        if op.get("status") != "confirmed":
            return False
        if op["kind"] == "produce":
            run = self.session.scalar(
                select(ProductionRun).where(
                    ProductionRun.operation_key == UUID(op["key"])
                )
            )
            return bool(
                run and run.completed_at is not None and run.output_lot_id is not None
            )
        return (
            datetime.fromisoformat(op["available_at"]) <= now
            and supply_resume_basis(
                self.session,
                self.period,
                op,
                usable_count=len(producer._available_units(op["sku"])),
            )
            is not None
        )

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
        elif op["status"] == "confirmed" and datetime.fromisoformat(
            op["available_at"]
        ) <= datetime.now(UTC):
            usable = len(producer._available_units(op["sku"]))
            basis = supply_resume_basis(
                self.session, self.period, op, usable_count=usable
            )
            if basis:
                state.estimated_spend += Decimal(op["cost"])
                if state.goal:
                    state.goal = state.goal | {
                        "last_supply": {
                            "key": op["key"],
                            "sku": op["sku"],
                            "quantity": op["quantity"],
                            "basis": basis,
                            "usable": usable,
                        }
                    }
                state.pending_operation = None
                self.session.commit()
                logger.info(
                    "Replenishment supply resumed: kit=%s sku=%s basis=%s usable=%s",
                    state.sku,
                    op["sku"],
                    basis,
                    usable,
                )
                return False
        if op["status"] == "in_flight":
            state.pending_operation = op | {"status": "uncertain"}
            self.session.commit()
        return True

    def _procurement_demand(self, state, goal, room, prices):
        priority = goal.get("priority", 2)
        loss_limit = (
            self.settings.replenishment_order_loss_limit if priority < 2 else Decimal(0)
        )
        goal_limit = max(
            Decimal(goal["revenue"]) * (1 + loss_limit),
            prices.get(state.sku, Decimal(0)) * max(state.target, goal["quantity"]),
        )
        return ProcurementDemand(
            sku=state.sku,
            quantity=goal["quantity"],
            deadline=goal["deadline"],
            expected_revenue=goal["revenue"],
            # This quote describes the remaining recipe, not its historical
            # purchases. Owned inputs already carry their consumption cost in
            # the planner; spending_room accounts for all past/pending outlay.
            # Charging cumulative spend again pins later orders to old costs.
            max_outlay=min(room, goal_limit),
            minimum_margin=0 if priority < 2 else Decimal("0.10"),
            maximum_loss_fraction=loss_limit,
            includes_committed=True,
            priority=priority,
        )

    @staticmethod
    def _needs_buffer_recovery(plan, sku):
        if plan.status == "waiting" and not plan.actions and plan.consumed_cost is None:
            return True
        return len(plan.actions) == 1 and (
            plan.actions[0].kind == "bid" and plan.actions[0].sku == sku
        )

    @staticmethod
    def _request_has_final_kit_bid(request, sku):
        decision = request.decision or {}
        return any(
            action.get("kind") == "bid" and action.get("sku") == sku
            for action in decision.get("actions", [])
        )

    def _buffer_recovery_goal(self, state, goal, product, price):
        shelf_horizon = (
            timedelta(hours=product.obsolescence / 2)
            if product.obsolescence
            else timedelta(0)
        )
        deadline = datetime.now(UTC) + max(
            timedelta(hours=2),
            timedelta(seconds=self.settings.replenishment_minimum_lifetime_seconds),
            shelf_horizon,
        )
        if self.period.ends_at:
            deadline = min(deadline, self.period.ends_at)
        # Recover one demand-sized lot first. Expanding a customer order to the
        # full stock target can make minimum input lots look unaffordable and
        # send the planner back to an unconfirmed final-kit bid. The producer
        # already rounds each manufacturing step to its official batch size;
        # any surplus inputs remain available for the next replenishment turn.
        quantity = min(200, goal["quantity"])
        recovery_for = goal.get("buffer_recovery_for") or {
            "quantity": goal["quantity"],
            "deadline": goal["deadline"],
            "revenue": goal["revenue"],
        }
        return {
            "sku": state.sku,
            "quantity": quantity,
            "deadline": deadline.isoformat(),
            # Keep whichever valuation is stronger: the real customer revenue
            # that justified this recovery, or the reference value of the
            # future stock. Replacing a high-value order with the lower buffer
            # price can incorrectly reject an otherwise profitable recipe.
            "revenue": str(max(Decimal(goal["revenue"]), price * quantity)),
            # Customer urgency keeps recipes ahead of speculative final-kit bids,
            # while the longer horizon lets those recipes actually complete.
            "priority": 1,
            "spend_started": str(state.estimated_spend),
            "buffer_recovery": True,
            "buffer_recovery_for": recovery_for,
        }

    async def _select_later_order(self, state, room, snapshot, offer_cache):
        goal = state.goal
        if goal.get("priority") != 1 or state.pending_operation or state.procurement_id:
            return False  # Do not abandon accepted or unresolved remote work.
        _, _, prices = snapshot
        tried = 0
        demands = self._order_demands(state.sku, datetime.now(UTC))
        for index, (item, _) in enumerate(demands):
            if (
                item.order.status != OrderStatus.RECEIVED
                or item.order.delivery_deadline is None
            ):
                continue
            deadline = item.order.delivery_deadline
            if self.period.ends_at:
                deadline = min(deadline, self.period.ends_at)
            if deadline <= datetime.fromisoformat(goal["deadline"]):
                continue
            quantity = 0
            revenue = Decimal(0)
            order_ids = []
            for later_item, missing in demands[index:]:
                if later_item.order.status != OrderStatus.RECEIVED:
                    continue
                take = min(missing, 200 - quantity)
                if take <= 0:
                    break
                quantity += take
                revenue += later_item.unit_price * take
                order_ids.append(str(later_item.order_id))
            candidate = goal | {
                "order_id": str(item.order_id),
                "order_ids": order_ids,
                "selected_at": datetime.now(UTC).isoformat(),
                "quantity": quantity,
                "deadline": deadline.isoformat(),
                "revenue": str(revenue),
            }
            demand = self._procurement_demand(state, candidate, room, prices)
            plan = await self.sourcing.preview(
                demand, snapshot=snapshot, offer_cache=offer_cache
            )
            tried += 1
            if (
                plan.status != "waiting"
                or plan.actions
                or plan.consumed_cost is not None
            ) and not self._needs_buffer_recovery(plan, state.sku):
                # Persist the choice across cycles/restarts. No acceptance or
                # purchasing here: the next cycle revalidates before execution.
                state.goal = candidate | {
                    "planned_outlay": str(plan.cost),
                    "estimated_consumption": str(plan.consumed_cost)
                    if plan.consumed_cost is not None
                    else None,
                    "max_outlay": str(demand.max_outlay),
                    "budget": {"remaining": str(room)},
                }
                self.session.commit()
                logger.info(
                    "Replenishment selected later order: "
                    "sku=%s order_id=%s deadline=%s",
                    state.sku,
                    item.order.external_order_id,
                    deadline.isoformat(),
                )
                return True
            if tried >= 3:
                break
        return False

    def _order_demands(self, sku, now):
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
        return sorted(
            selected,
            key=lambda pair: (
                pair[0].order.delivery_deadline or datetime.max.replace(tzinfo=UTC),
                pair[0].order.created_at,
                str(pair[0].order_id),
            ),
        )

    def _demand(
        self,
        sku,
        price,
        now,
        *,
        preferred_order_id=None,
        preferred_order_ids=None,
        preferred_selected_at=None,
    ):
        selected = self._order_demands(sku, now)
        if selected:
            if selected[0][0].order.status == OrderStatus.RECEIVED:
                wanted = set(preferred_order_ids or [])
                if not wanted and preferred_order_id:
                    wanted.add(preferred_order_id)
                preferred = [
                    pair for pair in selected if str(pair[0].order_id) in wanted
                ]
                if preferred:
                    chosen_at = (
                        datetime.fromisoformat(preferred_selected_at)
                        if preferred_selected_at
                        else max(pair[0].order.created_at for pair in preferred)
                    )
                    chosen_deadline = preferred[0][0].order.delivery_deadline
                    # Keep previously assessed infeasible work skipped, but do
                    # not hide a newly arrived OC with an earlier promise.
                    selected = [
                        pair
                        for pair in selected
                        if str(pair[0].order_id) in wanted
                        or (
                            pair[0].order.created_at > chosen_at
                            and pair[0].order.delivery_deadline is not None
                            and (
                                chosen_deadline is None
                                or pair[0].order.delivery_deadline < chosen_deadline
                            )
                        )
                    ]
            # A production turn has a mostly fixed lead time. Consolidate all
            # demand at the same priority so one recipe run can cover the
            # waiting queue instead of rebuilding the same component chain for
            # every individual order. The nearest deadline remains the bound;
            # later promises never extend the earliest customer's deadline.
            quantity = 0
            revenue = Decimal(0)
            for item, missing in selected:
                take = min(missing, 200 - quantity)
                if take <= 0:
                    break
                quantity += take
                revenue += item.unit_price * take
            first = selected[0][0]
            return (
                quantity,
                first.order.delivery_deadline,
                revenue,
                1 if first.order.status == OrderStatus.RECEIVED else 0,
            )
        return (
            0,
            None,
            price * self.settings.replenishment_target_stock if price else None,
            2,
        )
