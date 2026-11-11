"""Sell genuinely free stock; no procurement or artificial trade-count loop."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.clients.trades import TradesHTTPError
from app.models import (
    Invoice,
    InvoiceSide,
    InvoiceStatus,
    Location,
    Lot,
    MarketOffer,
    MarketOfferReservation,
    MarketRole,
    MarketTrade,
    Order,
    OrderItem,
    OrderStatus,
    ProcurementRequest,
    Product,
    ProductionRun,
    Unit,
)
from app.queries.recalls import blocked_lots
from app.schemas.farma_central import FarmaCentralProduct, FarmaCentralSpace
from app.schemas.procurement import ProcurementDemand
from app.schemas.surplus import SurplusPolicy, SurplusQuote
from app.schemas.trades import TradeOfferCreate
from app.services.inventory.movements import ProductMovementService
from app.services.inventory.sync import InventorySyncService
from app.services.production.locking import ProductionBusyError, production_lock

from .custody import ReservationGuard, TradeCustodyService
from .surplus_planning import offer_price, protected_stock, replacement_cost

STRATEGY = "surplus"
TERMINAL = {"cancelled", "expired", "closed", "failed"}


class SurplusSalesService:
    def __init__(
        self, inventory, trades, orders, prices, session, period, *, policy=None
    ):
        self.inventory, self.trades, self.prices = inventory, trades, prices
        self.session, self.period = session, period
        self.policy = policy or SurplusPolicy()
        self.custody = TradeCustodyService(
            session,
            farma_central=inventory,
            trades=trades,
            purchase_orders=orders,
            period=period,
        )

    def _managed(self):
        return self.session.scalars(
            select(MarketOffer)
            .where(
                MarketOffer.period_id == self.period.id,
                MarketOffer.strategy == STRATEGY,
            )
            .order_by(MarketOffer.created_at, MarketOffer.id)
        ).all()

    async def _snapshot(self, *, synchronize=True):
        if synchronize:
            await InventorySyncService(self.inventory, self.session).synchronize()
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
        spaces = TypeAdapter(list[FarmaCentralSpace]).validate_python(spaces_payload)
        prices = {}
        for record in price_payload:
            price = Decimal(str(record.get("price", 0)))
            if price.is_finite() and price > 0:
                prices[record["sku"]] = price
        cutoff = datetime.now(UTC) + timedelta(
            seconds=self.policy.minimum_lifetime_seconds
        )
        units = self.session.scalars(
            select(Unit)
            .join(Lot)
            .join(Product)
            .join(Location)
            .where(
                Unit.status == "available",
                Unit.effective_expires_at > cutoff,
                ~Unit.order_assignment.has(),
                Unit.lot_id.not_in(select(blocked_lots().c.lot_id)),
                ~Lot.external_lot_id.startswith("unreported:"),
                ~Lot.external_lot_id.startswith("production:"),
                Location.is_sellable.is_(True),
                ~Unit.market_reservations.any(
                    MarketOfferReservation.released_at.is_(None)
                ),
            )
            .order_by(Unit.effective_expires_at, Unit.id)
        ).all()
        free = defaultdict(list)
        for unit in units:
            product = catalog.get(unit.lot.product.sku)
            if product and (
                not product.storage.cold or unit.current_location.is_refrigerated
            ):
                free[product.sku].append(unit)
        physical = {sku: len(values) for sku, values in free.items()}
        # Include only our revocable reservations when deciding which stock to reclaim.
        for reservation in self.session.scalars(
            select(MarketOfferReservation)
            .join(MarketOffer)
            .where(
                MarketOffer.period_id == self.period.id,
                MarketOffer.strategy == STRATEGY,
                MarketOffer.status.not_in(TERMINAL),
                MarketOfferReservation.released_at.is_(None),
            )
        ):
            if (
                reservation.unit.status == "trade_reserved"
                and reservation.unit.effective_expires_at > cutoff
            ):
                sku = reservation.unit.lot.product.sku
                physical[sku] = physical.get(sku, 0) + 1
        demand = defaultdict(int)
        for product in catalog.values():
            if product.sellable:
                demand[product.sku] += self.policy.kit_buffer
        for item in self.session.scalars(
            select(OrderItem)
            .join(Order)
            .where(
                Order.status.in_(
                    [OrderStatus.RECEIVED, OrderStatus.ACCEPTED, OrderStatus.PAID]
                ),
            )
        ):
            if (
                item.order.delivery_deadline is None
                or item.order.delivery_deadline > datetime.now(UTC)
            ):
                demand[item.sku] += max(0, item.quantity - len(item.assigned_units))
        for request in self.session.scalars(
            select(ProcurementRequest).where(
                ProcurementRequest.period_id == self.period.id,
                ProcurementRequest.status.not_in(["acquired", "ready", "expired"]),
            )
        ):
            # Pending sourcing is additional protection, never stock capacity.
            need = ProcurementDemand.model_validate(request.demand)
            if need.deadline > datetime.now(UTC):
                demand[need.sku] += need.quantity + need.buffer
        protected = protected_stock(
            catalog, physical, demand, raw_buffer_batches=self.policy.raw_buffer_batches
        )
        dispatch = [s for s in spaces if s.check_out]
        dispatch_space = 0
        dispatch_cold = False
        if (
            len(dispatch) == 1
            and dispatch[0].total_space is not None
            and dispatch[0].used_space is not None
        ):
            dispatch_space = max(0, dispatch[0].total_space - dispatch[0].used_space)
            dispatch_cold = dispatch[0].cold
        quotes = []
        for sku in self.policy.skus:
            product = catalog.get(sku)
            count, keep = len(free[sku]), protected.get(sku, 0)
            quote = SurplusQuote(
                sku=sku,
                available=count,
                protected=keep,
                reference_price=prices.get(sku),
            )
            if product is None:
                quote.reason = "Product missing from current catalog"
            elif product.sellable:
                quote.reason = "Sellable kits are protected for customer fulfillment"
            else:
                cost = replacement_cost(catalog, sku)
                bought = self.session.scalar(
                    select(func.max(MarketTrade.unit_price)).where(
                        MarketTrade.period_id == self.period.id,
                        MarketTrade.role == MarketRole.BUYER,
                        MarketTrade.sku == sku,
                    )
                )
                if cost is not None:
                    cost = max(cost, bought or Decimal(0))
                quote.estimated_unit_cost = cost
                if cost is None or sku not in prices:
                    quote.reason = (
                        "Current reference price or replacement cost unavailable"
                    )
                elif product.storage.cold and not dispatch_cold:
                    quote.reason = (
                        "Dispatch does not preserve the required cold conditions"
                    )
                else:
                    quote.unit_price = offer_price(
                        prices[sku], cost, self.policy.minimum_margin
                    )
                    if quote.unit_price is None:
                        quote.reason = (
                            "Minimum margin is outside the current price band"
                        )
                    else:
                        quote.quantity = max(
                            0, min(count - keep, self.policy.lot_size, dispatch_space)
                        )
                        if not quote.quantity:
                            quote.reason = (
                                "Stock is protected or dispatch has no free capacity"
                            )
            quotes.append(quote)
        self.session.commit()
        return quotes, free

    def participation(self):
        roles = self.session.scalars(
            select(MarketTrade.role).where(MarketTrade.period_id == self.period.id)
        ).all()
        invoiced = self.session.scalar(
            select(func.count())
            .select_from(Invoice)
            .join(MarketTrade, Invoice.market_trade_id == MarketTrade.id)
            .where(
                MarketTrade.period_id == self.period.id,
                MarketTrade.role == MarketRole.SELLER,
                Invoice.side == InvoiceSide.SUPPLIER,
            )
        )
        paid = self.session.scalar(
            select(func.count())
            .select_from(Invoice)
            .join(MarketTrade, Invoice.market_trade_id == MarketTrade.id)
            .where(
                MarketTrade.period_id == self.period.id,
                MarketTrade.role == MarketRole.SELLER,
                Invoice.side == InvoiceSide.SUPPLIER,
                Invoice.status == InvoiceStatus.PAID,
            )
        )
        self.session.commit()
        return {
            "purchases": roles.count(MarketRole.BUYER),
            "sales": roles.count(MarketRole.SELLER),
            "target_per_role": 30,
            "sales_invoiced": invoiced,
            "sales_paid": paid,
        }

    async def _refresh_managed(self):
        now = datetime.now(UTC)
        for local in self._managed():
            if (
                local.status in {"expired", "cancelled", "failed"}
                or (local.status == "closed" and local.reconciled_at is not None)
                or not local.external_offer_id
                or (local.next_retry_at and local.next_retry_at > now)
            ):
                continue
            try:
                official = await self.trades.get_offer(local.external_offer_id)
                await self.custody.reconcile_offer(official)
                if official.status in TERMINAL:
                    local.reconciled_at = now
                local.last_error = local.http_status = local.next_retry_at = None
                self.session.commit()
            except SQLAlchemyError:
                raise
            except Exception as exc:
                self._remember_failure(local, exc)
                raise

    def _remember_failure(self, offer, exc):
        self.session.rollback()
        offer.last_error = type(exc).__name__
        offer.http_status = getattr(exc, "status_code", None)
        offer.next_retry_at = datetime.now(UTC) + timedelta(
            seconds=max(60, getattr(exc, "retry_after_seconds", None) or 0)
        )
        self.session.commit()

    async def _restore_released(self):
        failures = 0
        checkout_codes = {
            s.external_id
            for s in TypeAdapter(list[FarmaCentralSpace]).validate_python(
                await self.inventory.get_spaces()
            )
            if s.check_out
        }
        pending_ids = set(
            self.session.scalars(
                select(MarketOfferReservation.offer_id)
                .join(Unit)
                .join(MarketOffer)
                .where(
                    MarketOffer.period_id == self.period.id,
                    MarketOffer.strategy == STRATEGY,
                    MarketOfferReservation.released_at.is_not(None),
                    MarketOfferReservation.source_location_id.is_not(None),
                    Unit.status == "available",
                    Unit.current_location_id.in_(
                        select(Location.id).where(Location.code.in_(checkout_codes))
                    ),
                )
                .distinct()
            )
        )
        for offer in self._managed():
            if offer.id not in pending_ids:
                continue
            if offer.status not in {"expired", "cancelled", "failed"} or (
                offer.next_retry_at and offer.next_retry_at > datetime.now(UTC)
            ):
                continue
            for reservation in offer.reservations:
                unit = self.session.get(
                    Unit,
                    reservation.unit_id,
                    populate_existing=True,
                    with_for_update=True,
                )
                destination = (
                    self.session.get(Location, reservation.source_location_id)
                    if reservation.source_location_id
                    else None
                )
                if (
                    reservation.released_at is None
                    or unit.status != "available"
                    or unit.order_assignment
                    or unit.current_location.code not in checkout_codes
                    or destination is None
                    or not destination.is_sellable
                    or (
                        unit.lot.product.requires_refrigeration
                        and not destination.is_refrigerated
                    )
                    or unit.lot_id
                    in set(self.session.scalars(select(blocked_lots().c.lot_id)))
                ):
                    self.session.commit()
                    continue
                try:
                    await ProductMovementService(self.inventory, self.session).move(
                        product_id=unit.external_unit_id,
                        destination_store=destination.code,
                    )
                except SQLAlchemyError:
                    raise
                except Exception as exc:
                    self._remember_failure(offer, exc)
                    failures += 1
                    if getattr(exc, "status_code", None) in {401, 403, 429}:
                        raise
                    break
        self.session.commit()
        return failures

    async def cycle(self, *, execute=False):
        result = {
            "published": 0,
            "cancelled": 0,
            "deferred": 0,
            "failed": 0,
            "quotes": [],
        }
        if not execute:
            quotes, _ = await self._snapshot()
            return result | {
                "quotes": [q.model_dump(mode="json") for q in quotes],
                "participation": self.participation(),
            }
        try:
            cooldown = self.session.scalar(
                select(func.max(MarketOffer.next_retry_at)).where(
                    MarketOffer.period_id == self.period.id,
                    MarketOffer.strategy == STRATEGY,
                    MarketOffer.http_status.in_([401, 403, 429]),
                )
            )
            self.session.commit()
            if cooldown and cooldown > datetime.now(UTC):
                return result | {
                    "deferred": 1,
                    "reason": "Market rate/auth cooldown",
                    "participation": self.participation(),
                }
            await self._refresh_managed()
            with production_lock(self.session):
                if self.session.scalar(
                    select(ProductionRun.id)
                    .where(ProductionRun.completed_at.is_(None))
                    .limit(1)
                ):
                    return result | {
                        "deferred": 1,
                        "reason": "Production recovery pending",
                        "participation": self.participation(),
                    }
                await InventorySyncService(self.inventory, self.session).synchronize()
                result["failed"] += await self._restore_released()
                quotes, free = await self._snapshot(synchronize=False)
                result["quotes"] = [q.model_dump(mode="json") for q in quotes]
                managed = self._managed()
                now = datetime.now(UTC)
                in_period = self.period.starts_at <= now and (
                    self.period.ends_at is None or now < self.period.ends_at
                )
                for local in managed:
                    if (
                        local.status != "open"
                        or local.sku not in {q.sku for q in quotes}
                        or (local.next_retry_at and local.next_retry_at > now)
                    ):
                        continue
                    quote = next(q for q in quotes if q.sku == local.sku)
                    valid_units = self.session.scalar(
                        select(func.count())
                        .select_from(MarketOfferReservation)
                        .join(Unit)
                        .where(
                            MarketOfferReservation.offer_id == local.id,
                            MarketOfferReservation.released_at.is_(None),
                            Unit.status == "trade_reserved",
                            Unit.lot_id.not_in(select(blocked_lots().c.lot_id)),
                            Unit.effective_expires_at
                            > max(now + timedelta(seconds=60), local.expires_at or now),
                        )
                    )
                    unsafe = (
                        quote.available < quote.protected
                        or not in_period
                        or (
                            self.period.ends_at is not None
                            and local.expires_at is not None
                            and local.expires_at > self.period.ends_at
                        )
                        or valid_units != local.quantity
                    )
                    if quote.reference_price and quote.estimated_unit_cost is not None:
                        unsafe |= local.unit_price < quote.estimated_unit_cost / (
                            1 - self.policy.minimum_margin
                        ) or not quote.reference_price * Decimal(
                            "0.5"
                        ) <= local.unit_price <= quote.reference_price * Decimal("1.5")
                    if unsafe:
                        try:
                            await self.trades.cancel_offer(local.external_offer_id)
                            # A close can win the cancellation race; always re-read.
                            official = await self.trades.get_offer(
                                local.external_offer_id
                            )
                            await self.custody.reconcile_offer(official)
                        except SQLAlchemyError:
                            raise
                        except Exception as exc:
                            self._remember_failure(local, exc)
                            raise
                        result["cancelled"] += official.status in {
                            "cancelled",
                            "expired",
                        }
                if result["cancelled"]:
                    result["failed"] += await self._restore_released()
                    return result | {"participation": self.participation()}
                if not in_period or (
                    self.period.ends_at
                    and now + timedelta(seconds=3660) >= self.period.ends_at
                ):
                    return result | {
                        "deferred": 1,
                        "participation": self.participation(),
                    }
                # One new lot per cycle bounds staging and gives the producer its turn.
                last_publication = {
                    sku: max(
                        (o.created_at for o in managed if o.sku == sku),
                        default=datetime.min.replace(tzinfo=UTC),
                    )
                    for sku in self.policy.skus
                }
                for quote in sorted(
                    quotes, key=lambda q: (last_publication[q.sku], q.sku)
                ):
                    if not quote.quantity:
                        continue
                    if any(
                        o.sku == quote.sku and o.status not in TERMINAL for o in managed
                    ):
                        continue
                    if any(
                        o.sku == quote.sku and o.next_retry_at and o.next_retry_at > now
                        for o in managed
                    ):
                        continue
                    active_count = self.session.scalar(
                        select(func.count())
                        .select_from(MarketOffer)
                        .where(
                            MarketOffer.period_id == self.period.id,
                            MarketOffer.publisher_group_id == self.period.group_id,
                            MarketOffer.status.not_in(TERMINAL),
                        )
                    )
                    if active_count >= 15:
                        break
                    before_ids = [o.id for o in managed]
                    try:
                        await self.custody.publish_sell(
                            TradeOfferCreate(
                                side="sell",
                                sku=quote.sku,
                                quantity=quote.quantity,
                                unitPrice=quote.unit_price,
                            ),
                            current_unit_price=quote.reference_price,
                            guard=ReservationGuard(
                                [u.id for u in free[quote.sku]], quote.protected
                            ),
                            strategy=STRATEGY,
                            estimated_unit_cost=quote.estimated_unit_cost,
                        )
                        result["published"] += 1
                    except SQLAlchemyError:
                        raise
                    except Exception as exc:
                        self.session.rollback()
                        local = self.session.scalar(
                            select(MarketOffer).where(
                                MarketOffer.period_id == self.period.id,
                                MarketOffer.strategy == STRATEGY,
                                MarketOffer.id.not_in(before_ids),
                            )
                        )
                        if local:
                            if (
                                isinstance(exc, TradesHTTPError)
                                and exc.status_code in {400, 401, 403, 409, 422, 429}
                                and local.external_offer_id is None
                            ):
                                self.custody._release(local, status="failed")
                            local.last_error = type(exc).__name__
                            local.http_status = getattr(exc, "status_code", None)
                            local.next_retry_at = datetime.now(UTC) + timedelta(
                                seconds=max(
                                    60, getattr(exc, "retry_after_seconds", None) or 0
                                )
                            )
                            self.session.commit()
                        result["failed"] += 1
                    break
        except ProductionBusyError:
            result["deferred"] += 1
            result["reason"] = "Producer currently holds the packaging lock"
        return result | {"participation": self.participation()}
