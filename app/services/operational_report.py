"""Read-only operational observations; never retry business operations here."""

from datetime import datetime, timedelta

from sqlalchemy import exists, func, select

from app.models import (
    BillingOperationStatus,
    BillingSyncState,
    BillingTask,
    InboundEvent,
    Invoice,
    InvoiceSide,
    InvoiceStatus,
    Lot,
    MarketOffer,
    MarketOfferReservation,
    MessageDirection,
    MessagingDecision,
    MessagingMessage,
    Order,
    OrderStatus,
    Product,
    Recall,
    RecallStatus,
    Unit,
    WorkerHeartbeat,
)
from app.services.billing import BusinessMetricsService


def observe(session, period, *, now):
    def count(model, *conditions):
        return session.scalar(
            select(func.count()).select_from(model).where(*conditions)
        )

    def window(column):
        conditions = [column >= period.starts_at, column <= now]
        if period.ends_at:
            conditions.append(column < period.ends_at)
        return conditions

    alerts = []
    workers = []
    seen = set()
    for row in session.scalars(
        select(WorkerHeartbeat).where(WorkerHeartbeat.period_id == period.id)
    ):
        seen.add(row.worker_name)
        age = int((now - row.heartbeat_at).total_seconds())
        workers.append(
            {
                "name": row.worker_name,
                "age_seconds": age,
                "failures": row.consecutive_failures,
                "queue_depth": row.queue_depth,
            }
        )
        if age > 300 or row.consecutive_failures or row.last_success_at is None:
            alerts.append({"kind": "worker_unhealthy", "worker": row.worker_name})
    for name in sorted({"process_billing", "process_trades"} - seen):
        alerts.append({"kind": "worker_missing", "worker": name})

    pending = session.scalars(
        select(Invoice).where(
            Invoice.period_id == period.id,
            Invoice.side == InvoiceSide.CLIENT,
            Invoice.status == InvoiceStatus.PENDING,
        )
    )
    for row in pending:
        age = int((now - row.issued_at).total_seconds())
        if age >= 5 * 3600:
            alerts.append(
                {
                    "kind": "payment_overdue"
                    if age >= 6 * 3600
                    else "payment_due_soon",
                    "invoice_id": row.external_invoice_id,
                    "age_seconds": age,
                }
            )
    stuck = count(
        BillingTask,
        BillingTask.period_id == period.id,
        BillingTask.status != BillingOperationStatus.CONFIRMED,
        BillingTask.updated_at < now - timedelta(minutes=5),
    )
    if stuck:
        alerts.append({"kind": "billing_tasks_stalled", "count": stuck})

    cutoff = min(now, period.ends_at) if period.ends_at else now
    metrics = (
        BusinessMetricsService(session, period)
        .calculate(now=now, financial_as_of=cutoff)
        .model_dump(mode="json")
    )
    if metrics["invoicing"]["missing"] or metrics["invoicing"]["uncertain"]:
        alerts.append(
            {"kind": "sales_without_confirmed_invoice", **metrics["invoicing"]}
        )
    if (
        metrics["reconciliation"]["age_seconds"] is None
        or metrics["reconciliation"]["age_seconds"] > 300
    ):
        alerts.append({"kind": "billing_observations_stale"})

    inquiries = count(
        InboundEvent,
        *window(InboundEvent.received_at),
        InboundEvent.decision == MessagingDecision.INQUIRY,
        ~exists(
            select(MessagingMessage.id).where(
                MessagingMessage.inbound_event_id == InboundEvent.id,
                MessagingMessage.direction == MessageDirection.OUTBOUND,
                MessagingMessage.period_id == period.id,
            )
        ),
    )
    recalls = count(
        Recall, *window(Recall.issued_at), Recall.status != RecallStatus.COMPLETED
    )
    if inquiries:
        alerts.append({"kind": "inquiries_without_text", "count": inquiries})
    if recalls:
        alerts.append({"kind": "recalls_pending", "count": recalls})
    waiting = session.execute(
        select(
            Order.external_order_id, Order.created_at, Order.delivery_deadline
        ).where(
            *window(Order.created_at),
            Order.status.in_([OrderStatus.RECEIVED, OrderStatus.ACCEPTED]),
        )
    ).all()
    orders = [
        {
            "order_id": identity,
            "age_seconds": int((now - created).total_seconds()),
            "deadline": deadline.isoformat() if deadline else None,
        }
        for identity, created, deadline in waiting
    ]
    for row in orders:
        if row["deadline"] and datetime.fromisoformat(
            row["deadline"]
        ) <= now + timedelta(minutes=10):
            alerts.append({"kind": "order_deadline_near", "order_id": row["order_id"]})

    quotas = [
        {
            "stream": row.stream,
            "http_status": row.http_status,
            "next_retry_at": row.next_retry_at.isoformat()
            if row.next_retry_at
            else None,
        }
        for row in session.scalars(
            select(BillingSyncState).where(
                BillingSyncState.period_id == period.id,
                BillingSyncState.http_status == 429,
            )
        )
    ]
    market_429 = count(
        MarketOffer, MarketOffer.period_id == period.id, MarketOffer.http_status == 429
    )
    if quotas or market_429:
        alerts.append(
            {
                "kind": "remote_rate_limit",
                "market_offers": market_429,
                "billing_streams": quotas,
            }
        )
    stock = [
        {"sku": sku, "status": status, "units": quantity}
        for sku, status, quantity in session.execute(
            select(Product.sku, Unit.status, func.count(Unit.id))
            .outerjoin(Lot, Lot.product_id == Product.id)
            .outerjoin(Unit, Unit.lot_id == Lot.id)
            .group_by(Product.sku, Unit.status)
            .order_by(Product.sku, Unit.status)
        )
    ]
    offers = [
        {"status": status, "count": quantity}
        for status, quantity in session.execute(
            select(MarketOffer.status, func.count())
            .where(MarketOffer.period_id == period.id)
            .group_by(MarketOffer.status)
        )
    ]
    reserved = count(
        MarketOfferReservation,
        MarketOfferReservation.offer_id.in_(
            select(MarketOffer.id).where(MarketOffer.period_id == period.id)
        ),
        MarketOfferReservation.released_at.is_(None),
    )
    return {
        "metrics": metrics,
        "workers": workers,
        "alerts": alerts,
        "stock_local_by_sku_status": stock,
        "offers": offers,
        "market_reserved_units": reserved,
        "waiting_orders": orders,
        "orders_by_channel": [
            {
                "source": source.value,
                "status": status.value,
                "official_status": official.value if official else None,
                "count": quantity,
            }
            for source, status, official, quantity in session.execute(
                select(Order.source, Order.status, Order.external_status, func.count())
                .where(*window(Order.created_at))
                .group_by(Order.source, Order.status, Order.external_status)
            )
        ],
        "channels": [
            {"source": source.value, "status": status.value, "count": quantity}
            for source, status, quantity in session.execute(
                select(InboundEvent.source, InboundEvent.status, func.count())
                .where(*window(InboundEvent.received_at))
                .group_by(InboundEvent.source, InboundEvent.status)
            )
        ],
    }
