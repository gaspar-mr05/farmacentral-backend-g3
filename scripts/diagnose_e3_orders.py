"""Export local E3 evidence in a PostgreSQL read-only snapshot; no API/LLM calls."""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import case, create_engine, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models import (
    CustodyEvent,
    InboundEvent,
    InboundEventSource,
    Location,
    Lot,
    MarketOffer,
    MarketTrade,
    Order,
    OrderStatus,
    ProcurementRequest,
    Product,
    ProductionRun,
    ReplenishmentState,
    Unit,
)
from app.services.billing.periods import billing_period
from app.services.orders.processing import OrderProcessingService


def pick(value, names):
    return {name: value[name] for name in names if name in (value or {})}


def build_report(session, settings, *, limit=30):
    period = billing_period(session, settings)
    now = datetime.now(UTC)
    truncated = {}

    def window(column):
        result = [column >= period.starts_at, column <= now]
        if period.ends_at:
            result.append(column < period.ends_at)
        return result

    def rows(name, statement):
        values = session.scalars(statement.limit(limit + 1)).all()
        truncated[name] = len(values) > limit
        return values[:limit]

    processing = OrderProcessingService(None, session, settings=settings)
    active_order = Order.status.in_([OrderStatus.RECEIVED, OrderStatus.ACCEPTED])
    orders = []
    for order in rows(
        "orders",
        select(Order)
        .where(*window(Order.created_at), Order.external_order_id.is_not(None))
        .order_by(
            active_order.desc(),
            case((active_order, Order.delivery_deadline)).asc().nullslast(),
            Order.decision_at.desc().nullslast(),
            Order.created_at.desc(),
            Order.id,
        ),
    ):
        items = []
        for item in order.items:
            confirmed = (
                processing._confirmed_production(item.sku, now, order.delivery_deadline)
                if order.delivery_deadline
                else None
            )
            items.append(
                {
                    "sku": item.sku,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "assigned": len(item.assigned_units),
                    "stock_for_deadline": processing._available_stock(
                        item.sku, order.delivery_deadline
                    )
                    if order.delivery_deadline
                    else None,
                    "confirmed_production_for_deadline": confirmed,
                    "committed_to_other_orders": processing._committed_quantity(
                        item.sku, order.id
                    ),
                }
            )
        orders.append(
            {
                "local_id": order.id,
                "oc": order.external_order_id,
                "source": order.source,
                "status": order.status,
                "official_status": order.external_status,
                "created_at": order.created_at,
                "deadline": order.delivery_deadline,
                "decision_at": order.decision_at,
                "reason": order.decision_reason,
                "items": items,
            }
        )

    replenishment = []
    for row in rows(
        "replenishment",
        select(ReplenishmentState)
        .where(ReplenishmentState.period_id == period.id)
        .order_by(ReplenishmentState.sku),
    ):
        replenishment.append(
            {
                "sku": row.sku,
                "minimum": row.minimum,
                "target": row.target,
                "goal": pick(
                    row.goal,
                    (
                        "order_id",
                        "quantity",
                        "deadline",
                        "priority",
                        "revenue",
                        "planned_outlay",
                        "estimated_consumption",
                        "max_outlay",
                        "budget",
                        "last_supply",
                    ),
                ),
                "pending_operation": pick(
                    row.pending_operation,
                    (
                        "kind",
                        "sku",
                        "quantity",
                        "required",
                        "needed",
                        "received_before",
                        "market_receipts_before",
                        "status",
                        "available_at",
                        "cost",
                    ),
                ),
                "procurement_id": row.procurement_id,
                "estimated_spend": row.estimated_spend,
                "error": row.last_error,
                "http_status": row.http_status,
                "next_retry_at": row.next_retry_at,
                "updated_at": row.updated_at,
            }
        )

    sourcing = [
        {
            "id": row.id,
            "offer_local_id": row.market_offer_id,
            "status": row.status,
            "demand": pick(
                row.demand,
                (
                    "sku",
                    "quantity",
                    "deadline",
                    "expected_revenue",
                    "other_costs",
                    "max_outlay",
                    "minimum_margin",
                    "maximum_loss_fraction",
                    "includes_committed",
                    "priority",
                ),
            ),
            "error": row.last_error,
            "http_status": row.http_status,
            "next_retry_at": row.next_retry_at,
            "selected_input": (row.decision or {}).get("next_action_sku"),
            "latest_plan": pick(
                (row.decision or {}).get("latest_preview") or row.decision,
                ("status", "cost", "consumed_cost", "seconds", "reason", "actions"),
            ),
        }
        for row in rows(
            "sourcing",
            select(ProcurementRequest)
            .where(
                ProcurementRequest.period_id == period.id,
                ProcurementRequest.status.not_in(
                    ["acquired", "ready", "expired", "superseded"]
                ),
            )
            .order_by(ProcurementRequest.created_at),
        )
    ]

    production = [
        {
            "id": row.id,
            "sku": row.expected_sku,
            "quantity": row.expected_quantity,
            "requested_at": row.requested_at,
            "available_at": row.available_at,
            "past_availability_without_completion": bool(
                row.available_at and row.available_at < now
            ),
            "output_lot_id": row.output_lot_id,
        }
        for row in rows(
            "production",
            select(ProductionRun)
            .where(
                *window(ProductionRun.requested_at),
                ProductionRun.completed_at.is_(None),
            )
            .order_by(ProductionRun.requested_at),
        )
    ]

    offers = [
        {
            "id": row.id,
            "official_offer_id": row.external_offer_id,
            "oc": row.external_order_id,
            "sku": row.sku,
            "side": row.side,
            "publisher": row.publisher_group_id,
            "quantity": row.quantity,
            "unit_price": row.unit_price,
            "status": row.status,
            "expires_at": row.expires_at,
            "http_status": row.http_status,
            "next_retry_at": row.next_retry_at,
            "error": row.last_error,
        }
        for row in rows(
            "offers",
            select(MarketOffer)
            .where(
                MarketOffer.period_id == period.id, MarketOffer.reconciled_at.is_(None)
            )
            .order_by(MarketOffer.created_at),
        )
    ]

    messages = []
    for event in rows(
        "messages",
        select(InboundEvent)
        .where(
            *window(InboundEvent.received_at),
            InboundEvent.source == InboundEventSource.MESSAGING,
        )
        .order_by(InboundEvent.received_at.desc()),
    ):
        error = event.last_error or ""
        delivery = (event.resolution_evidence or {}).get("delivery") or {}
        messages.append(
            {
                "message_id": event.external_event_id,
                "test": event.external_event_id.startswith("test_"),
                "received_at": event.received_at,
                "status": event.status,
                "decision": event.decision,
                "responded_at": event.responded_at,
                "oc": event.external_order_id,
                "consulted_oc": event.consulted_external_order_id,
                "sku": event.interpreted_sku,
                "quantity": event.interpreted_quantity,
                "interpreted_deadline": event.interpreted_delivery_date,
                "attempts": event.attempt_count,
                "next_retry_at": event.next_retry_at,
                "error_flags": {
                    "http_404": "404" in error,
                    "http_409": "409" in error or "already answered" in error,
                    "quota": "quota" in error.lower(),
                    "expired": "expired" in error.lower(),
                },
                "delivery": {
                    phase: pick(
                        delivery.get(phase),
                        ("state", "attemptedAt", "confirmedAt", "lastHttpStatus"),
                    )
                    for phase in ("classification", "reply")
                },
            }
        )

    stock = [
        {
            "sku": sku,
            "status": status,
            "location": location,
            "sellable_location": sellable,
            "units": count,
            "earliest_expiry": expiry,
        }
        for sku, status, location, sellable, count, expiry in session.execute(
            select(
                Product.sku,
                Unit.status,
                Location.code,
                Location.is_sellable,
                func.count(),
                func.min(Unit.effective_expires_at),
            )
            .select_from(Unit)
            .join(Lot)
            .join(Product)
            .join(Location, Unit.current_location_id == Location.id)
            .group_by(Product.sku, Unit.status, Location.code, Location.is_sellable)
            .order_by(Product.sku, Location.code, Unit.status)
        )
    ]
    custody = [
        {
            "oc": trade.external_order_id,
            "role": trade.role,
            "sku": trade.sku,
            "quantity": trade.quantity,
            "custody_events": count,
            "offer_id": trade.external_offer_id,
            "closed_at": trade.closed_at,
        }
        for trade, count in session.execute(
            select(MarketTrade, func.count(CustodyEvent.id))
            .outerjoin(CustodyEvent, CustodyEvent.market_trade_id == MarketTrade.id)
            .where(MarketTrade.period_id == period.id)
            .group_by(MarketTrade.id)
            .order_by(MarketTrade.closed_at)
        )
    ]
    return {
        "observed_at": now,
        "scope": "local_read_only_snapshot_no_remote_calls",
        "environment": period.environment,
        "group": period.group_id,
        "api_host": urlsplit(str(settings.farma_central_base_url)).hostname,
        "period": {
            "id": period.id,
            "code": period.code,
            "start": period.starts_at,
            "end": period.ends_at,
        },
        "policy": {
            "stock_wait_seconds": settings.external_order_stock_wait_seconds,
            "dispatch_margin_seconds": settings.external_order_dispatch_margin_seconds,
            "spending_limit": settings.replenishment_spending_limit,
            "order_loss_limit": settings.replenishment_order_loss_limit,
        },
        "order_counts": [
            {"source": source, "status": status, "count": count}
            for source, status, count in session.execute(
                select(Order.source, Order.status, func.count())
                .where(*window(Order.created_at), Order.external_order_id.is_not(None))
                .group_by(Order.source, Order.status)
            )
        ],
        "orders": orders,
        "replenishment": replenishment,
        "sourcing": sourcing,
        "production": production,
        "offers": offers,
        "messages": messages,
        "stock": stock,
        "market_custody": custody,
        "truncated": truncated,
        "limits": [
            "Local state may lag official services; no remote services queried",
            "Stock by status/location is not guaranteed free usable inventory",
            "Detail sections are capped; order_counts covers the whole period",
        ],
    }


def read_report(engine, settings, *, limit=30):
    with Session(engine) as session:
        session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        session.execute(text("SET LOCAL statement_timeout = '15s'"))
        report = build_report(session, settings, limit=limit)
        session.rollback()
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30, choices=range(1, 201))
    args = parser.parse_args(argv)
    engine = None
    try:
        settings = get_settings()
        engine = create_engine(
            settings.database_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 3},
        )
        # Refuse overwrite and symlinks; do not send a private report to stdout.
        descriptor = os.open(
            args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            try:
                report = read_report(engine, settings, limit=args.limit)
            except Exception as exc:
                json.dump(
                    {"status": "failed", "error_type": type(exc).__name__}, handle
                )
                raise
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        print(
            json.dumps(
                {
                    "status": "captured",
                    "output": str(args.output),
                    "truncated": report["truncated"],
                }
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
