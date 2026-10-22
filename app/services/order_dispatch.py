from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.clients.purchase_orders import PurchaseOrdersClient
from app.models import (
    CustodyEvent,
    CustodyEventType,
    ExternalOrderStatus,
    Location,
    Order,
    OrderItem,
    OrderStatus,
    OrderUnit,
    Unit,
)
from app.schemas.farma_central import FarmaCentralSpace, FarmaCentralUnit
from app.services.custody.events import record_move
from app.services.external_order_state import apply_official_state
from app.services.orders import OrderNotFoundError, get_order


class InvalidDispatchStateError(Exception):
    pass


class OrderDispatchService:
    def __init__(
        self,
        client: FarmaCentralClient,
        session: Session,
        purchase_orders: PurchaseOrdersClient | None = None,
    ) -> None:
        self._client = client
        self._session = session
        self._purchase_orders = purchase_orders

    async def dispatch(self, order_id: UUID, *, reconcile_only: bool = False) -> Order:
        destination = None
        try:
            while True:
                order = self._get_locked_order(order_id)
                if order.status == OrderStatus.DISPATCHED:
                    self._session.commit()
                    return get_order(self._session, order_id)
                if (
                    order.external_order_id is not None
                    and order.external_status
                    in {
                        ExternalOrderStatus.EXPIRED,
                        ExternalOrderStatus.CANCELLED,
                        ExternalOrderStatus.REJECTED,
                    }
                    and not any(
                        a.dispatched_at is None
                        and a.dispatch_expected_quantity is not None
                        for item in order.items
                        for a in item.assigned_units
                    )
                ):
                    self._session.commit()
                    return get_order(self._session, order_id)
                pending = self._pending_assignments(order)
                if reconcile_only:
                    pending = [
                        a for a in pending if a.dispatch_expected_quantity is not None
                    ]
                if not pending:
                    if self._is_complete(order):
                        order.status = OrderStatus.DISPATCHED
                    self._session.commit()
                    return get_order(self._session, order_id)
                if destination is None:
                    destination = await self._get_destination()
                assignment = pending[0]
                if order.external_order_id is not None:
                    if self._purchase_orders is None:
                        raise InvalidDispatchStateError(
                            "An official order client is required for external dispatch"
                        )
                    if assignment.dispatch_expected_quantity is None:
                        await self._prepare_external_dispatch(assignment, destination)
                        # Persist the intent, then reacquire the order and unit locks.
                        # Another worker can safely resume it if this process stops.
                        self._session.commit()
                        continue
                    await self._resolve_external_dispatch(
                        assignment, destination, reconcile_only=reconcile_only
                    )
                else:
                    await self._move_to_dispatch(assignment, destination)
                    self._record_dispatch(assignment, destination)
                if self._is_complete(order):
                    order.status = OrderStatus.DISPATCHED
                self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _is_complete(order: Order) -> bool:
        return bool(order.items) and all(
            len(item.assigned_units) == item.quantity
            and all(a.dispatched_at is not None for a in item.assigned_units)
            for item in order.items
        )

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
        is_external_order = order.external_order_id is not None and order.status in {
            OrderStatus.ACCEPTED,
            OrderStatus.EXPIRED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
        if order.status != OrderStatus.PAID and not is_external_order:
            raise InvalidDispatchStateError("Order must be paid before dispatch")
        has_intent = any(
            a.dispatched_at is None and a.dispatch_expected_quantity is not None
            for item in order.items
            for a in item.assigned_units
        )
        if not order.items or (
            not has_intent
            and any(len(item.assigned_units) != item.quantity for item in order.items)
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
        pending.sort(key=lambda a: a.dispatch_expected_quantity is None)
        units = self._session.scalars(
            select(Unit)
            .where(Unit.id.in_([assignment.unit_id for assignment in pending]))
            .order_by(Unit.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if any(
            unit.status != "reserved"
            or (unit.effective_expires_at <= datetime.now(UTC) and not has_intent)
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

    async def _move_to_dispatch(
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

    async def _prepare_external_dispatch(
        self, assignment: OrderUnit, destination: Location
    ) -> None:
        order = assignment.order_item.order
        detail = await self._purchase_orders.get_order(order.external_order_id)
        confirmed = sum(
            a.dispatched_at is not None
            for item in order.items
            for a in item.assigned_units
        )
        if (
            detail.state != ExternalOrderStatus.ACCEPTED
            or detail.dispatched_quantity != confirmed
            or (
                order.delivery_deadline and order.delivery_deadline <= datetime.now(UTC)
            )
        ):
            raise InvalidDispatchStateError("Official order is not ready for dispatch")
        await self._move_to_dispatch(assignment, destination)
        assignment.dispatch_expected_quantity = confirmed + 1

    async def _resolve_external_dispatch(
        self, assignment: OrderUnit, destination: Location, *, reconcile_only: bool
    ) -> None:
        order = assignment.order_item.order
        detail = await self._purchase_orders.get_order(order.external_order_id)
        expected = assignment.dispatch_expected_quantity
        at_dispatch = await self._is_at_dispatch(
            assignment.unit,
            destination,
            assignment.order_item.sku,
            require_complete=True,
        )
        if detail.dispatched_quantity == expected and not at_dispatch:
            self._record_dispatch(assignment, destination)
            apply_official_state(
                self._session, order, ExternalOrderStatus(detail.state)
            )
            return
        if detail.dispatched_quantity != expected - 1 or not at_dispatch:
            raise InvalidDispatchStateError("The pending dispatch cannot be reconciled")
        if detail.state != ExternalOrderStatus.ACCEPTED:
            if detail.state not in {
                ExternalOrderStatus.EXPIRED,
                ExternalOrderStatus.CANCELLED,
                ExternalOrderStatus.REJECTED,
            }:
                raise InvalidDispatchStateError("Unexpected official dispatch state")
            assignment.dispatch_expected_quantity = None
            apply_official_state(
                self._session, order, ExternalOrderStatus(detail.state)
            )
            return
        if reconcile_only or (
            order.delivery_deadline and order.delivery_deadline <= datetime.now(UTC)
        ):
            raise InvalidDispatchStateError("The dispatch is still unresolved")
        await self._client.dispatch_product(
            assignment.unit.external_unit_id, order.external_order_id
        )
        self._record_dispatch(assignment, destination)

    def _record_dispatch(self, assignment: OrderUnit, destination: Location) -> None:
        unit = assignment.unit
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
        assignment.dispatch_expected_quantity = None

    async def _is_at_dispatch(
        self,
        unit: Unit,
        destination: Location,
        sku: str,
        *,
        require_complete: bool = False,
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
        found = any(
            record.external_id == unit.external_unit_id
            and record.store_id == destination.code
            and record.sku == sku
            and record.expires_at > datetime.now(UTC)
            for record in records
        )
        if (
            require_complete
            and not found
            and (len(records) >= 200 or unit.effective_expires_at <= datetime.now(UTC))
        ):
            raise InvalidDispatchStateError(
                "Dispatch inventory cannot prove unit absence"
            )
        return found
