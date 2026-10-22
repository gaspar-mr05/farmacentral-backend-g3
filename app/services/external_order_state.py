from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExternalOrderStatus, Order, OrderStatus, Unit


def apply_official_state(
    session: Session, order: Order, status: ExternalOrderStatus
) -> None:
    order.external_status = status
    terminal_statuses = {
        ExternalOrderStatus.EXPIRED: OrderStatus.EXPIRED,
        ExternalOrderStatus.CANCELLED: OrderStatus.CANCELLED,
        ExternalOrderStatus.REJECTED: OrderStatus.REJECTED,
    }
    if status not in terminal_statuses:
        return
    order.status = terminal_statuses[status]
    releasable = [
        assignment
        for item in order.items
        for assignment in item.assigned_units
        if assignment.dispatched_at is None
        and assignment.dispatch_expected_quantity is None
    ]
    units = session.scalars(
        select(Unit)
        .where(Unit.id.in_([assignment.unit_id for assignment in releasable]))
        .order_by(Unit.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for unit in units:
        if unit.status == "reserved":
            unit.status = (
                "available"
                if unit.effective_expires_at > datetime.now(UTC)
                else "expired"
            )
    for assignment in releasable:
        assignment.order_item.assigned_units.remove(assignment)
