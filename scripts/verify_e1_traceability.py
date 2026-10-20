"""Verify a persisted E1 delivery without changing data or calling external APIs."""

import argparse
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import CustodyEventType, LotOrigin, Unit
from app.services.traceability.service import TraceabilityService


def verify_delivery(session: Session, external_unit_id: str) -> dict:
    unit = session.scalar(select(Unit).where(Unit.external_unit_id == external_unit_id))
    if unit is None:
        raise ValueError("Unit was not found in the configured database")
    assignment = unit.order_assignment
    if unit.status != "dispatched" or assignment is None:
        raise ValueError("Unit has no completed order assignment")
    if assignment.dispatched_at is None:
        raise ValueError("The assignment has no dispatch timestamp")

    order = assignment.order_item.order
    if not order.buyer_name:
        raise ValueError("The delivery has no recorded customer")

    service = TraceabilityService(session)
    trace = service.get_traceability(unit.lot.external_lot_id)
    if unit.lot.origin == LotOrigin.OWN_PRODUCTION and not trace.ancestors:
        raise ValueError("Own production has no recorded input lots")
    if not any(
        delivery.external_unit_id == external_unit_id and delivery.order_id == order.id
        for delivery in trace.deliveries
    ):
        raise ValueError("Lot traceability does not include this delivery")

    for ancestor in trace.ancestors:
        upstream = service.get_traceability(ancestor.external_lot_id)
        if not any(
            delivery.external_unit_id == external_unit_id
            for delivery in upstream.deliveries
        ):
            raise ValueError("An input lot cannot be traced downstream to the customer")

    events = sorted(unit.custody_events, key=lambda event: event.occurred_at)
    event_types = {event.event_type for event in events}
    if CustodyEventType.DISPATCHED not in event_types:
        raise ValueError("The delivery has no dispatch custody event")
    if not event_types & {CustodyEventType.PRODUCED, CustodyEventType.RECEIVED}:
        raise ValueError("The unit has no receipt or production custody event")

    return {
        "unit_id": external_unit_id,
        "lot_id": unit.lot.external_lot_id,
        "sku": unit.lot.product.sku,
        "order_id": str(order.id),
        "customer": order.buyer_name,
        "dispatched_at": assignment.dispatched_at.isoformat(),
        "ancestor_lots": [lot.external_lot_id for lot in trace.ancestors],
        "production_links": len(trace.production_links),
        "custody": [
            {
                "event": event.event_type.value,
                "occurred_at": event.occurred_at.isoformat(),
                "from": event.from_location.code if event.from_location else None,
                "to": event.to_location.code if event.to_location else None,
            }
            for event in events
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-id", required=True)
    args = parser.parse_args()
    with SessionLocal() as session:
        try:
            result = verify_delivery(session, args.unit_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
