from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.models import (
    CustodyEvent,
    CustodyEventType,
    Location,
    Order,
    OrderItem,
    OrderStatus,
    OrderUnit,
    Unit,
)
from app.schemas.farma_central import FarmaCentralSpace, FarmaCentralUnit
from app.services.custody.events import record_move
from app.services.orders import OrderNotFoundError, get_order


class InvalidDispatchStateError(Exception):
    pass


class OrderDispatchService:
    def __init__(self, client: FarmaCentralClient, session: Session) -> None:
        self._client = client
        self._session = session

    async def dispatch(self, order_id: UUID) -> Order:
        destination = None
        try:
            while True:
                order = self._get_locked_order(order_id)
                if order.status == OrderStatus.DISPATCHED:
                    self._session.commit()
                    return get_order(self._session, order_id)
                pending = self._pending_assignments(order)
                if not pending:
                    order.status = OrderStatus.DISPATCHED
                    self._session.commit()
                    return get_order(self._session, order_id)
                if destination is None:
                    destination = await self._get_destination()
                await self._dispatch_unit(pending[0], destination)
                if len(pending) == 1:
                    order.status = OrderStatus.DISPATCHED
                self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def _get_locked_order(self, order_id: UUID) -> Order:
        order = self._session.scalar(
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.items).selectinload(OrderItem.assigned_units))
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if order is None:
            raise OrderNotFoundError(f"Order {order_id} was not found")
        return order

    def _pending_assignments(self, order: Order) -> list[OrderUnit]:
        if order.status != OrderStatus.PAID:
            raise InvalidDispatchStateError("Order must be paid before dispatch")
        if not order.items or any(
            len(item.assigned_units) != item.quantity for item in order.items
        ):
            raise InvalidDispatchStateError(
                "Order must be completely assigned before dispatch"
            )
        pending = [
            assignment
            for item in order.items
            for assignment in item.assigned_units
            if assignment.dispatched_at is None
        ]
        units = self._session.scalars(
            select(Unit)
            .where(Unit.id.in_([assignment.unit_id for assignment in pending]))
            .order_by(Unit.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if any(
            unit.status != "reserved" or unit.effective_expires_at <= datetime.now(UTC)
            for unit in units
        ):
            raise InvalidDispatchStateError(
                "Assigned units must be reserved and unexpired"
            )
        return pending

    async def _get_destination(self) -> Location:
        payload = await self._client.get_spaces()
        if not isinstance(payload, list):
            raise FarmaCentralInvalidResponseError("Spaces must be a list")
        try:
            spaces = [FarmaCentralSpace.model_validate(record) for record in payload]
        except ValueError as exc:
            raise FarmaCentralInvalidResponseError("Invalid spaces") from exc
        destinations = [space for space in spaces if space.check_out]
        if len(destinations) != 1:
            raise InvalidDispatchStateError("Exactly one dispatch space is required")
        location = self._session.scalar(
            select(Location).where(Location.code == destinations[0].external_id)
        )
        if location is None:
            raise InvalidDispatchStateError("Synchronize inventory before dispatch")
        return location

    async def _dispatch_unit(
        self, assignment: OrderUnit, destination: Location
    ) -> None:
        unit = assignment.unit
        sku = assignment.order_item.sku
        if not await self._is_at_dispatch(unit, destination, sku):
            await self._client.move_product(unit.external_unit_id, destination.code)
            if not await self._is_at_dispatch(unit, destination, sku):
                raise FarmaCentralInvalidResponseError(
                    "Transferred unit was not found at dispatch"
                )
        if unit.current_location_id != destination.id:
            record_move(self._session, unit=unit, to_location_id=destination.id)
        self._session.add(
            CustodyEvent(
                unit_id=unit.id,
                event_type=CustodyEventType.DISPATCHED,
                from_location_id=destination.id,
                to_location_id=None,
                order_id=assignment.order_item.order_id,
            )
        )
        unit.status = "dispatched"
        assignment.dispatched_at = datetime.now(UTC)

    async def _is_at_dispatch(
        self, unit: Unit, destination: Location, sku: str
    ) -> bool:
        payload = await self._client.get_space_products(
            destination.code, sku, limit=200
        )
        if not isinstance(payload, list):
            raise FarmaCentralInvalidResponseError("Dispatch inventory must be a list")
        try:
            records = [FarmaCentralUnit.model_validate(record) for record in payload]
        except ValueError as exc:
            raise FarmaCentralInvalidResponseError(
                "Invalid dispatch inventory"
            ) from exc
        return any(
            record.external_id == unit.external_unit_id
            and record.store_id == destination.code
            and record.sku == sku
            and record.expires_at > datetime.now(UTC)
            for record in records
        )
