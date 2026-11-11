from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from app.clients.farma_central import FarmaCentralClient
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
from app.schemas.trades import TradeOffer, TradeOfferCreate
from app.services.custody.events import record_move


class InvalidTradeStateError(Exception):
    pass


class InsufficientTradeStockError(InvalidTradeStateError):
    pass


class TradeCustodyService:
    """Coordinates durable local reservations with official market custody."""

    def __init__(
        self,
        session: Session,
        *,
        farma_central: FarmaCentralClient,
        trades: TradesClient,
        period: BusinessPeriod,
    ) -> None:
        self._session = session
        self._farma_central = farma_central
        self._trades = trades
        self._period = period
        self._group_id = period.group_id

    async def publish_sell(
        self,
        request: TradeOfferCreate,
        *,
        current_unit_price: Decimal,
    ) -> TradeOffer:
        if request.side != MarketOfferSide.SELL.value:
            raise InvalidTradeStateError("Only sell offers reserve local stock")

        offer = self._reserve_offer(
            request,
            publisher_group_id=self._group_id,
            status="preparing",
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
        trade = self._upsert_trade(offer, role=MarketRole.SELLER)
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
        trade = self._upsert_trade(offer, role=MarketRole.BUYER)
        self._session.commit()
        return accepted, trade

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
                "Trade unit evidence must match the complete offer quantity"
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
    ) -> MarketOffer:
        now = datetime.now(UTC)
        units = list(
            self._session.scalars(
                select(Unit)
                .join(Lot, Unit.lot_id == Lot.id)
                .join(Product, Lot.product_id == Product.id)
                .join(Location, Unit.current_location_id == Location.id)
                .where(
                    Product.sku == request.sku,
                    Unit.status == "available",
                    Unit.effective_expires_at > now,
                    Unit.lot_id.not_in(select(blocked_lots().c.lot_id)),
                    Location.is_sellable.is_(True),
                    ~exists().where(
                        MarketOfferReservation.unit_id == Unit.id,
                        MarketOfferReservation.released_at.is_(None),
                    ),
                )
                .order_by(Unit.effective_expires_at, Unit.id)
                .limit(request.quantity)
                .with_for_update(of=Unit, skip_locked=True)
            )
        )
        if len(units) != request.quantity:
            self._session.rollback()
            raise InsufficientTradeStockError(
                f"Insufficient unreserved stock for {request.sku}"
            )

        offer = MarketOffer(
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
            MarketOfferReservation(unit=unit, reserved_at=now) for unit in units
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

    def _ready_for_market(self, offer, destination_id):
        # Keep these unit locks through the market request. A recall that commits
        # first is observed here; a concurrent recall cannot change locked stock.
        units = self._reserved_units(offer)
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
            offer.sku != official.sku
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

    def _upsert_trade(self, offer: MarketOffer, *, role: MarketRole) -> MarketTrade:
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
            quantity=offer.quantity,
            unit_price=offer.unit_price,
            closed_at=datetime.now(UTC),
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
        if len(active) != trade.quantity:
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
        if offer.side != side.value:
            raise InvalidTradeStateError(f"Expected a {side.value} offer")
        if offer.publisher_group is None:
            raise InvalidTradeStateError("Offer publisher is required for custody")
        if offer.publisher_group == self._group_id:
            raise InvalidTradeStateError("A group cannot accept its own offer")
