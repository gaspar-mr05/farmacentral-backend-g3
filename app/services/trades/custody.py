from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from app.clients.farma_central import FarmaCentralClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.trades import TradesClient
from app.models import (
    BusinessPeriod,
    CustodyEvent,
    CustodyEventType,
    Location,
    Lot,
    MarketOffer,
    MarketOfferReservation,
    MarketOfferSide,
    MarketRole,
    MarketTrade,
    Product,
    Unit,
)
from app.queries.recalls import blocked_lots
from app.schemas.farma_central import FarmaCentralSpace
from app.schemas.purchase_orders import PurchaseOrder, PurchaseOrderState
from app.schemas.trades import TradeOffer, TradeOfferCreate
from app.services.billing.queue import enqueue_sale
from app.services.custody.events import record_move


class InvalidTradeStateError(Exception):
    pass


class InsufficientTradeStockError(InvalidTradeStateError):
    pass


@dataclass(frozen=True)
class ReservationGuard:
    unit_ids: Sequence[UUID]
    keep_quantity: int
    minimum_lifetime_seconds: float = 3660


class TradeCustodyService:
    """Coordinates durable local reservations with official market custody."""

    def __init__(
        self,
        session: Session,
        *,
        farma_central: FarmaCentralClient,
        trades: TradesClient,
        purchase_orders: PurchaseOrdersClient,
        period: BusinessPeriod,
    ) -> None:
        self._session = session
        self._farma_central = farma_central
        self._trades = trades
        self._purchase_orders = purchase_orders
        self._period = period
        self._group_id = period.group_id

    async def publish_sell(
        self,
        request: TradeOfferCreate,
        *,
        current_unit_price: Decimal,
        guard: ReservationGuard | None = None,
        strategy: str | None = None,
        estimated_unit_cost: Decimal | None = None,
    ) -> TradeOffer:
        if request.side != MarketOfferSide.SELL.value:
            raise InvalidTradeStateError("Only sell offers reserve local stock")

        request.validate_market_price(current_unit_price)
        offer = self._reserve_offer(
            request,
            publisher_group_id=self._group_id,
            status="preparing",
            guard=guard,
            strategy=strategy,
            estimated_unit_cost=estimated_unit_cost,
        )
        try:
            destination_id = await self._move_reservations_to_dispatch(offer)
            self._ready_for_market(offer, destination_id, guard=guard)
        except InvalidTradeStateError:
            self._release(offer, status="failed")
            self._session.commit()
            raise
        except Exception:
            offer.status = "movement_uncertain"
            self._session.commit()
            raise

        try:
            official = await self._trades.create_offer(
                request,
                current_unit_price=current_unit_price,
            )
            self._apply_official_offer(offer, official)
        except Exception:
            # The request may have reached the market. Keep custody until a GET
            # proves that the offer never opened or is no longer open.
            offer.status = "creation_uncertain"
            self._session.commit()
            raise

        self._session.commit()
        return official

    async def accept_buy(self, official: TradeOffer) -> tuple[TradeOffer, MarketTrade]:
        """Accept another group's buy offer as seller."""
        self._validate_counterparty_offer(official, side=MarketOfferSide.BUY)
        offer = self._reserve_offer(
            TradeOfferCreate(
                side=official.side,
                sku=official.sku,
                quantity=official.quantity,
                unitPrice=official.unit_price,
            ),
            publisher_group_id=official.publisher_group,
            external_offer_id=official.id,
            status="accepting",
        )
        try:
            destination_id = await self._move_reservations_to_dispatch(offer)
            self._ready_for_market(offer, destination_id)
        except InvalidTradeStateError:
            self._release(offer, status="failed")
            self._session.commit()
            raise
        except Exception:
            offer.status = "movement_uncertain"
            self._session.commit()
            raise

        try:
            accepted = await self._trades.accept_offer(official.id)
            self._apply_official_offer(offer, accepted)
        except Exception:
            offer.status = "acceptance_uncertain"
            self._session.commit()
            raise

        offer.counterparty_group_id = self._group_id
        self._session.commit()
        trade = await self._confirmed_trade(offer, accepted, role=MarketRole.SELLER)
        # A commercial closure is not yet evidence of which units were transferred.
        # Keep reservations until record_trade_units receives official IDs.
        self._session.commit()
        return accepted, trade

    async def accept_sell(self, official: TradeOffer) -> tuple[TradeOffer, MarketTrade]:
        """Accept a sell offer; receipt is recorded after inventory sync."""
        self._validate_counterparty_offer(official, side=MarketOfferSide.SELL)
        offer = self._store_offer(official, status="accepting")
        try:
            accepted = await self._trades.accept_offer(official.id)
            self._apply_official_offer(offer, accepted)
        except Exception:
            offer.status = "acceptance_uncertain"
            self._session.commit()
            raise

        offer.counterparty_group_id = self._group_id
        self._session.commit()
        trade = await self._confirmed_trade(offer, accepted, role=MarketRole.BUYER)
        self._session.commit()
        return accepted, trade

    async def reconcile_offer(self, official: TradeOffer) -> MarketTrade | None:
        """Observe one known offer; never accept, cancel, or dispatch it again."""
        offer = self._get_offer(official.id)
        self._apply_official_offer(offer, official)
        if official.status in {"cancelled", "expired"}:
            self._release(offer, status=official.status)
            self._session.commit()
            return None
        if official.status != "closed":
            self._session.commit()
            return None
        if (
            official.closed_at is None
            or not official.order_id
            or official.counterparty_group is None
        ):
            raise InvalidTradeStateError("Closed offer lacks official closure evidence")
        if (
            offer.publisher_group_id != self._group_id
            and official.counterparty_group != self._group_id
        ):
            # Someone else won the acceptance race. Our units were not sold.
            self._release(offer, status=official.status)
            self._session.commit()
            return None
        seller = (
            official.publisher_group
            if official.side == "sell"
            else official.counterparty_group
        )
        role = MarketRole.SELLER if seller == self._group_id else MarketRole.BUYER
        self._session.commit()  # Preserve orderId even if its subsequent GET fails.
        trade = await self._confirmed_trade(offer, official, role=role)
        if trade.role == MarketRole.SELLER:
            enqueue_sale(self._session, self._period, trade.external_order_id)
        self._session.commit()
        return trade

    def record_trade_units(
        self,
        trade_id: UUID,
        external_unit_ids: Sequence[str],
    ) -> MarketTrade:
        """Attach official unit evidence to one closure without dispatching again."""
        trade = self._session.scalar(
            select(MarketTrade).where(MarketTrade.id == trade_id).with_for_update()
        )
        if trade is None or trade.period_id != self._period.id:
            raise InvalidTradeStateError("Market trade was not found in this period")
        unit_ids = tuple(dict.fromkeys(external_unit_ids))
        if len(unit_ids) != trade.quantity:
            raise InvalidTradeStateError(
                "Trade unit evidence must match the official order quantity"
            )

        offer = (
            self._get_offer(trade.external_offer_id)
            if trade.role == MarketRole.SELLER
            else None
        )
        units = list(
            self._session.scalars(
                select(Unit)
                .join(Lot, Unit.lot_id == Lot.id)
                .join(Product, Lot.product_id == Product.id)
                .where(
                    Unit.external_unit_id.in_(unit_ids),
                    Product.sku == trade.sku,
                )
                .order_by(Unit.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(units) != trade.quantity:
            raise InvalidTradeStateError(
                "Synchronize all transferred units before recording custody"
            )

        recorded_ids = set(
            self._session.scalars(
                select(CustodyEvent.unit_id).where(
                    CustodyEvent.market_trade_id == trade.id
                )
            )
        )
        requested_ids = {unit.id for unit in units}
        if recorded_ids:
            if recorded_ids != requested_ids:
                raise InvalidTradeStateError(
                    "Trade already has different unit custody evidence"
                )
            return trade

        if trade.role == MarketRole.SELLER:
            other_reservation = self._session.scalar(
                select(MarketOfferReservation.id)
                .where(
                    MarketOfferReservation.unit_id.in_(requested_ids),
                    MarketOfferReservation.offer_id != offer.id,
                    MarketOfferReservation.released_at.is_(None),
                )
                .limit(1)
            )
            blocked = set(self._session.scalars(select(blocked_lots().c.lot_id)))
            if other_reservation or any(
                unit.status in {"reserved", "consumed", "quarantined", "recall_pending"}
                or unit.lot_id in blocked
                for unit in units
            ):
                raise InvalidTradeStateError(
                    "Official unit evidence conflicts with blocked or reserved stock"
                )
            self._record_sold_units(trade, offer, units)
        else:
            blocked = set(self._session.scalars(select(blocked_lots().c.lot_id)))
            for unit in units:
                self._session.add(
                    CustodyEvent(
                        unit_id=unit.id,
                        event_type=CustodyEventType.RECEIVED,
                        from_location_id=None,
                        to_location_id=unit.current_location_id,
                        market_trade_id=trade.id,
                        occurred_at=trade.closed_at,
                    )
                )
                # A unit sold earlier can later re-enter with the same official ID.
                if unit.status == "dispatched":
                    unit.status = (
                        "recall_pending" if unit.lot_id in blocked else "available"
                    )
        self._session.commit()
        return trade

    def release_offer(self, external_offer_id: str, *, status: str) -> MarketOffer:
        if status.lower() not in {"cancelled", "canceled", "expired", "failed"}:
            raise InvalidTradeStateError(
                "Reservations require a confirmed terminal offer state"
            )
        offer = self._get_offer(external_offer_id)
        self._release(offer, status=status)
        self._session.commit()
        return offer

    def _reserve_offer(
        self,
        request: TradeOfferCreate,
        *,
        publisher_group_id: int,
        status: str,
        external_offer_id: str | None = None,
        guard: ReservationGuard | None = None,
        strategy: str | None = None,
        estimated_unit_cost: Decimal | None = None,
    ) -> MarketOffer:
        if guard and (guard.keep_quantity < 0 or guard.minimum_lifetime_seconds < 0):
            raise ValueError(
                "Reservation guard quantities/lifetime must be nonnegative"
            )
        now = datetime.now(UTC)
        cutoff = now + timedelta(seconds=guard.minimum_lifetime_seconds if guard else 0)
        units = list(
            self._session.scalars(
                select(Unit)
                .join(Lot, Unit.lot_id == Lot.id)
                .join(Product, Lot.product_id == Product.id)
                .join(Location, Unit.current_location_id == Location.id)
                .where(
                    Product.sku == request.sku,
                    Unit.status == "available",
                    Unit.effective_expires_at > cutoff,
                    ~Unit.order_assignment.has(),
                    Unit.lot_id.not_in(select(blocked_lots().c.lot_id)),
                    Location.is_sellable.is_(True),
                    Unit.id.in_(guard.unit_ids) if guard else True,
                    ~exists().where(
                        MarketOfferReservation.unit_id == Unit.id,
                        MarketOfferReservation.released_at.is_(None),
                    ),
                )
                .order_by(Unit.effective_expires_at, Unit.id)
                .limit(request.quantity + (guard.keep_quantity if guard else 0))
                .with_for_update(of=Unit, skip_locked=True)
            )
        )
        if len(units) != request.quantity + (guard.keep_quantity if guard else 0):
            self._session.rollback()
            raise InsufficientTradeStockError(
                f"Insufficient unreserved stock for {request.sku}"
            )

        units = units[: request.quantity]
        offer = MarketOffer(
            strategy=strategy,
            estimated_unit_cost=estimated_unit_cost,
            protected_quantity=guard.keep_quantity if guard else None,
            period_id=self._period.id,
            external_offer_id=external_offer_id,
            side=MarketOfferSide(request.side),
            publisher_group_id=publisher_group_id,
            sku=request.sku,
            quantity=request.quantity,
            unit_price=request.unit_price,
            status=status,
        )
        offer.reservations = [
            MarketOfferReservation(
                unit=unit, source_location_id=unit.current_location_id, reserved_at=now
            )
            for unit in units
        ]
        for unit in units:
            unit.status = "trade_reserved"
        self._session.add(offer)
        self._session.commit()
        return offer

    async def _move_reservations_to_dispatch(self, offer: MarketOffer) -> UUID:
        destination = await self._dispatch_location()
        for reservation in offer.reservations:
            locked = self._reserved_units(offer)
            unit = next(unit for unit in locked if unit.id == reservation.unit_id)
            if unit.current_location_id == destination.id:
                continue
            await self._farma_central.move_product(
                unit.external_unit_id, destination.code
            )
            record_move(self._session, unit=unit, to_location_id=destination.id)
            self._session.commit()
        return destination.id

    def _reserved_units(self, offer):
        self._session.execute(
            select(MarketOffer.id).where(MarketOffer.id == offer.id).with_for_update()
        )
        units = list(
            self._session.scalars(
                select(Unit)
                .join(MarketOfferReservation)
                .where(
                    MarketOfferReservation.offer_id == offer.id,
                    MarketOfferReservation.released_at.is_(None),
                )
                .order_by(Unit.id)
                .with_for_update(of=Unit)
                .execution_options(populate_existing=True)
            )
        )
        blocked = set(self._session.scalars(select(blocked_lots().c.lot_id)))
        now = datetime.now(UTC)
        if len(units) != offer.quantity or any(
            unit.status != "trade_reserved"
            or unit.effective_expires_at <= now
            or unit.lot_id in blocked
            for unit in units
        ):
            raise InvalidTradeStateError(
                "Reserved stock expired, was blocked or changed"
            )
        return units

    def _ready_for_market(self, offer, destination_id, *, guard=None):
        # Keep these unit locks through the market request. A recall that commits
        # first is observed here; a concurrent recall cannot change locked stock.
        units = self._reserved_units(offer)
        if guard and any(
            unit.effective_expires_at
            <= datetime.now(UTC) + timedelta(seconds=guard.minimum_lifetime_seconds)
            for unit in units
        ):
            raise InvalidTradeStateError(
                "Reserved stock no longer covers the offer lifetime"
            )
        if any(unit.current_location_id != destination_id for unit in units):
            raise InvalidTradeStateError("Reserved units have not reached dispatch")

    async def _dispatch_location(self) -> Location:
        payload = await self._farma_central.get_spaces()
        if not isinstance(payload, list):
            raise InvalidTradeStateError("Farma Central spaces must be a list")
        try:
            spaces = [FarmaCentralSpace.model_validate(record) for record in payload]
        except ValueError as exc:
            raise InvalidTradeStateError(
                "Farma Central returned invalid spaces"
            ) from exc
        dispatch_spaces = [space for space in spaces if space.check_out]
        if len(dispatch_spaces) != 1:
            raise InvalidTradeStateError("Exactly one dispatch space is required")
        location = self._session.scalar(
            select(Location).where(Location.code == dispatch_spaces[0].external_id)
        )
        if location is None:
            raise InvalidTradeStateError("Synchronize inventory before trading")
        return location

    def _store_offer(self, official: TradeOffer, *, status: str) -> MarketOffer:
        existing = self._session.scalar(
            select(MarketOffer).where(
                MarketOffer.period_id == self._period.id,
                MarketOffer.external_offer_id == official.id,
            )
        )
        if existing is not None:
            return existing
        offer = MarketOffer(
            period_id=self._period.id,
            external_offer_id=official.id,
            side=MarketOfferSide(official.side),
            publisher_group_id=official.publisher_group,
            sku=official.sku,
            quantity=official.quantity,
            unit_price=official.unit_price,
            status=status,
            opened_at=official.created_at,
            expires_at=official.expires_at,
        )
        self._session.add(offer)
        self._session.commit()
        return offer

    def _apply_official_offer(self, offer: MarketOffer, official: TradeOffer) -> None:
        if (
            (
                offer.external_offer_id is not None
                and offer.external_offer_id != official.id
            )
            or offer.publisher_group_id != official.publisher_group
            or offer.sku != official.sku
            or offer.quantity != official.quantity
            or offer.side.value != official.side
            or Decimal(offer.unit_price) != official.unit_price
        ):
            raise InvalidTradeStateError("Official offer does not match local intent")
        offer.external_offer_id = official.id
        offer.external_order_id = official.order_id
        offer.status = official.status
        offer.opened_at = official.created_at
        offer.expires_at = official.expires_at
        offer.counterparty_group_id = official.counterparty_group

    async def _confirmed_trade(
        self, offer: MarketOffer, official: TradeOffer, *, role: MarketRole
    ) -> MarketTrade:
        if official.status != "closed" or official.closed_at is None:
            raise InvalidTradeStateError("Official closure time and state are required")
        order = await self._purchase_orders.get_order(offer.external_order_id)
        seller = (
            official.publisher_group
            if official.side == "sell"
            else official.counterparty_group
        )
        buyer = (
            official.counterparty_group
            if official.side == "sell"
            else official.publisher_group
        )
        if (
            self._group_id not in {seller, buyer}
            or seller == buyer
            or (role == MarketRole.SELLER) != (seller == self._group_id)
            or official.closed_at < self._period.starts_at
            or (
                self._period.ends_at is not None
                and official.closed_at >= self._period.ends_at
            )
        ):
            raise InvalidTradeStateError("Market closure outside group/period scope")
        if (
            order.id != offer.external_order_id
            or order.state != PurchaseOrderState.FULFILLED
            or order.supplier_id != str(seller)
            or order.customer_id != str(buyer)
            or order.sku != offer.sku
            or order.quantity > offer.quantity
            or order.dispatched_quantity != order.quantity
            or order.unit_price != offer.unit_price
        ):
            raise InvalidTradeStateError("Official order does not match market closure")
        return self._upsert_trade(
            offer, role=role, order=order, closed_at=official.closed_at
        )

    def _upsert_trade(
        self,
        offer: MarketOffer,
        *,
        role: MarketRole,
        order: PurchaseOrder,
        closed_at: datetime,
    ) -> MarketTrade:
        if not offer.external_offer_id or not offer.external_order_id:
            raise InvalidTradeStateError("Closed offer is missing official identifiers")
        trade = self._session.scalar(
            select(MarketTrade).where(
                MarketTrade.period_id == self._period.id,
                MarketTrade.external_offer_id == offer.external_offer_id,
            )
        )
        if trade is not None:
            return trade
        counterparty = (
            offer.publisher_group_id
            if offer.publisher_group_id != self._group_id
            else offer.counterparty_group_id
        )
        if counterparty is None or counterparty == self._group_id:
            raise InvalidTradeStateError("Trade counterparty is missing")
        trade = MarketTrade(
            period_id=self._period.id,
            external_offer_id=offer.external_offer_id,
            external_order_id=offer.external_order_id,
            role=role,
            counterparty_id=str(counterparty),
            sku=offer.sku,
            quantity=order.quantity,
            unit_price=order.unit_price,
            closed_at=closed_at,
        )
        self._session.add(trade)
        self._session.flush()
        return trade

    def _record_sold_units(
        self, trade: MarketTrade, offer: MarketOffer, units: Sequence[Unit]
    ) -> None:
        active = [
            reservation
            for reservation in offer.reservations
            if reservation.released_at is None
        ]
        if len(active) != offer.quantity:
            raise InvalidTradeStateError("Offer reservation is incomplete")
        # The official IDs can differ from our original FEFO selection.
        # Release the old intent, but record only the confirmed transfer evidence.
        self._release(offer, status=offer.status)
        for unit in units:
            unit.status = "dispatched"
            self._session.add(
                CustodyEvent(
                    unit_id=unit.id,
                    event_type=CustodyEventType.DISPATCHED,
                    from_location_id=unit.current_location_id,
                    to_location_id=None,
                    market_trade_id=trade.id,
                    occurred_at=trade.closed_at,
                )
            )

    def _release(self, offer: MarketOffer, *, status: str) -> None:
        # Re-read under unit locks so a concurrent recall cannot be undone.
        list(
            self._session.scalars(
                select(Unit)
                .join(MarketOfferReservation)
                .where(
                    MarketOfferReservation.offer_id == offer.id,
                    MarketOfferReservation.released_at.is_(None),
                )
                .order_by(Unit.id)
                .with_for_update(of=Unit)
                .execution_options(populate_existing=True)
            )
        )
        now = datetime.now(UTC)
        blocked = set(self._session.scalars(select(blocked_lots().c.lot_id)))
        offer.status = status
        for reservation in offer.reservations:
            if reservation.released_at is not None:
                continue
            reservation.released_at = now
            if reservation.unit.status == "trade_reserved":
                reservation.unit.status = (
                    "recall_pending"
                    if reservation.unit.lot_id in blocked
                    else "expired"
                    if reservation.unit.effective_expires_at <= now
                    else "available"
                )

    def _get_offer(self, external_offer_id: str) -> MarketOffer:
        offer = self._session.scalar(
            select(MarketOffer)
            .where(
                MarketOffer.period_id == self._period.id,
                MarketOffer.external_offer_id == external_offer_id,
            )
            .options(
                selectinload(MarketOffer.reservations).selectinload(
                    MarketOfferReservation.unit
                )
            )
            .with_for_update()
        )
        if offer is None:
            raise InvalidTradeStateError("Market offer was not found")
        return offer

    def _validate_counterparty_offer(
        self, offer: TradeOffer, *, side: MarketOfferSide
    ) -> None:
        if offer.status != "open":
            raise InvalidTradeStateError("Only open offers can be accepted")
        if offer.side != side.value:
            raise InvalidTradeStateError(f"Expected a {side.value} offer")
        if offer.publisher_group is None:
            raise InvalidTradeStateError("Offer publisher is required for custody")
        if offer.publisher_group == self._group_id:
            raise InvalidTradeStateError("A group cannot accept its own offer")
