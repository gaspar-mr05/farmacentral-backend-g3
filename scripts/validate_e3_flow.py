"""Observe selected development flows without mutating business or local data."""

import argparse
import asyncio
import json
import os
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.clients.billing import BillingClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.trades import TradesClient
from app.core.config import get_settings
from app.models import (
    BusinessPeriod,
    CustodyEvent,
    InboundEvent,
    InboundEventSource,
    Invoice,
    InvoiceSide,
    InvoiceStatus,
    MarketRole,
    MarketTrade,
    MessageDirection,
    MessagingMessage,
    Order,
    OrderStatus,
    Recall,
)
from app.services.production.lineage import has_official_lineage
from scripts.e2_checks import matches_official_order, result
from scripts.validate_e2_flow import database_check, observe

DEV_HOST = "dev.proyecto.2026-2.tallerdeintegracion.cl"


async def check_sale(session, identity, orders, group):
    row = session.scalar(select(Order).where(Order.external_order_id == identity))
    if row.status != OrderStatus.DISPATCHED:
        return result(
            "pending",
            "La OC aún no tiene despacho completo",
            local_status=row.status.value,
        )
    official = await orders.get_order(identity)
    if not matches_official_order(row, official, group):
        return result("failed", "La OC oficial no coincide con la venta local")
    if (
        official.state.value != "cumplida"
        or official.dispatched_quantity != official.quantity
    ):
        return result("pending", "Falta cumplimiento oficial completo")
    units = []
    for item in row.items:
        if len(item.assigned_units) != item.quantity:
            return result("failed", "Asignaciones incompletas de la venta")
        for assignment in item.assigned_units:
            if (
                assignment.dispatched_at is None
                or assignment.dispatched_at > official.delivery_deadline
            ):
                return result("failed", "Despacho sin confirmar dentro del plazo")
            if not has_official_lineage(assignment.unit.lot, allow_external=True):
                return result(
                    "pending",
                    "Falta identidad oficial o linaje de unidades compradas/producidas",
                )
            units.append(
                {
                    "unit_id": assignment.unit.external_unit_id,
                    "batch": assignment.unit.lot.external_lot_id,
                    "sku": item.sku,
                    "dispatched_at": assignment.dispatched_at.isoformat(),
                }
            )
    return result(
        "passed" if units else "failed",
        "Venta y entregas observadas",
        channel=row.source.value,
        order_id=identity,
        units=units,
    )


def require_development(settings, environment):
    urls = [
        str(settings.farma_central_base_url),
        settings.effective_purchase_orders_base_url,
        settings.effective_trades_base_url,
        settings.effective_billing_url,
        settings.effective_billing_wsdl_url,
        settings.effective_messaging_base_url,
    ]
    if environment != "dev" or any(
        urlsplit(url).hostname != DEV_HOST or urlsplit(url).scheme != "https"
        for url in urls
    ):
        raise ValueError(
            "Use explicit dev environment and official development endpoints"
        )


async def check_invoice(session, billing, period, order_id, side, cache):
    row = session.scalar(
        select(Invoice).where(
            Invoice.period_id == period.id,
            Invoice.external_order_id == order_id,
            Invoice.side == side,
        )
    )
    if row is None:
        return result(
            "pending", "Falta asociación local verificable entre OC y factura"
        )
    start = row.issued_at.astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    matches = []
    for state in (InvoiceStatus.PAID, InvoiceStatus.PENDING):
        key = (side, state, start)
        if key not in cache:
            cache[key] = await billing.get_invoices(
                side=side,
                status=state,
                from_date=start,
                to_date=start + timedelta(days=1),
            )
        matches.extend(
            invoice
            for invoice in cache[key]
            if invoice.invoice_id == row.external_invoice_id
        )
    if not matches:
        return result("pending", "Factura local no observada en el servicio oficial")
    if len(matches) != 1:
        return result("failed", "ID de factura repetido entre observaciones oficiales")
    official = matches[0]
    party = official.supplier_id if side == InvoiceSide.CLIENT else official.client_id
    own = official.client_id if side == InvoiceSide.CLIENT else official.supplier_id
    if (
        own != str(period.group_id)
        or party != row.counterparty_id
        or official.principal != row.principal
    ):
        return result(
            "failed", "Identidad o principal oficial no coincide con el registro local"
        )
    paid = official.status == InvoiceStatus.PAID
    return result(
        "passed" if side == InvoiceSide.SUPPLIER or paid else "pending",
        "Estado de factura observado; no se emite ni paga",
        invoice_id=official.invoice_id,
        order_id=order_id,
        side=side.value,
        official_status=official.status.value,
        description=official.description,
        observed_at=datetime.now(UTC).isoformat(),
        principal=str(official.principal),
        interest=str(official.interest),
        total=str(official.total),
    )


def check_message(event):
    if event.responded_at is None:
        return result("pending", "No hay fecha de respuesta confirmada")
    delivery = (event.resolution_evidence or {}).get("delivery", {})
    deadline = datetime.fromisoformat(
        event.payload["receivedAt"].replace("Z", "+00:00")
    ) + timedelta(minutes=30)
    for name in ("classification", "reply"):
        phase = delivery.get(name, {})
        if phase.get("state") != "confirmed" or not phase.get("confirmedAt"):
            return result("pending", "Falta confirmación de ambas fases", phase=name)
        if datetime.fromisoformat(phase["confirmedAt"]) >= deadline:
            return result(
                "failed", "Confirmación fuera de los treinta minutos", phase=name
            )
    reply = delivery["reply"]
    if (
        reply.get("to") != event.payload["sender"]["phone"]
        or reply.get("replyTo") != event.external_event_id
    ):
        return result("failed", "Destinatario o mensaje original no coincide")
    return result(
        "passed",
        "Auditoría local de ambas fases; recepción visual en chat aún no verificada",
        message_id=event.external_event_id,
        order_id=event.consulted_external_order_id,
        method=(event.resolution_evidence or {}).get("method"),
        responded_at=event.responded_at.isoformat() if event.responded_at else None,
    )


async def inspect(session, period, args, settings, orders, billing, trades):
    checks = {"database": database_check(session)}
    cache = {}
    for identity in args.order_id:
        row = session.scalar(select(Order).where(Order.external_order_id == identity))
        if (
            row is None
            or row.created_at < period.starts_at
            or (period.ends_at and row.created_at >= period.ends_at)
        ):
            checks[f"order:{identity}"] = result(
                "pending", "OC no registrada en el período seleccionado"
            )
            continue
        checks[f"order:{identity}"] = await observe(
            check_sale, session, identity, orders, period.group_id
        )
        checks[f"invoice:{identity}"] = await observe(
            check_invoice,
            session,
            billing,
            period,
            identity,
            InvoiceSide.SUPPLIER,
            cache,
        )
    for identity in args.trade_id:
        trade = session.get(MarketTrade, identity)
        if trade is None or trade.period_id != period.id:
            checks[f"trade:{identity}"] = result(
                "pending", "Operación de mercado no registrada en este período"
            )
            continue
        official = await trades.get_offer(trade.external_offer_id)
        order = await orders.get_order(trade.external_order_id)
        evidence = session.scalars(
            select(CustodyEvent).where(CustodyEvent.market_trade_id == trade.id)
        ).all()
        units = [
            {
                "unit_id": entry.unit.external_unit_id,
                "batch": entry.unit.lot.external_lot_id,
                "sku": entry.unit.lot.product.sku,
                "event_type": entry.event_type.value,
                "occurred_at": entry.occurred_at.isoformat(),
            }
            for entry in evidence
        ]
        seller = (
            str(period.group_id)
            if trade.role == MarketRole.SELLER
            else trade.counterparty_id
        )
        buyer = (
            trade.counterparty_id
            if trade.role == MarketRole.SELLER
            else str(period.group_id)
        )
        valid = (
            official.status == "closed"
            and official.order_id == trade.external_order_id
            and order.supplier_id == seller
            and order.customer_id == buyer
            and order.sku == trade.sku
            and order.quantity == trade.quantity
            and order.unit_price == trade.unit_price
            and order.dispatched_quantity == trade.quantity
            and order.state.value == "cumplida"
        )
        checks[f"trade:{identity}"] = result(
            "passed"
            if valid
            and len(units) == trade.quantity
            and all(unit["sku"] == trade.sku for unit in units)
            else "pending",
            "Cierre, OC y evidencia de unidades",
            offer_id=trade.external_offer_id,
            order_id=trade.external_order_id,
            role=trade.role.value,
            unit_evidence=units,
            quantity=trade.quantity,
        )
        side = (
            InvoiceSide.CLIENT
            if trade.role == MarketRole.BUYER
            else InvoiceSide.SUPPLIER
        )
        checks[f"trade-invoice:{identity}"] = await observe(
            check_invoice,
            session,
            billing,
            period,
            trade.external_order_id,
            side,
            cache,
        )
    for identity in args.message_id:
        event = session.scalar(
            select(InboundEvent).where(
                InboundEvent.source == InboundEventSource.MESSAGING,
                InboundEvent.external_event_id == identity,
                InboundEvent.id.in_(
                    select(MessagingMessage.inbound_event_id).where(
                        MessagingMessage.period_id == period.id,
                        MessagingMessage.group_id == period.group_id,
                        MessagingMessage.environment == "dev",
                        MessagingMessage.direction == MessageDirection.INBOUND,
                    )
                ),
            )
        )
        checks[f"message:{identity}"] = (
            check_message(event)
            if event
            else result("pending", "Mensaje no recibido localmente")
        )
    for identity in args.recall_id:
        recall = session.scalar(select(Recall).where(Recall.recall_id == identity))
        checks[f"recall:{identity}"] = result(
            "passed"
            if recall
            and recall.reported_at
            and recall.reported_at
            < min(recall.deadline, recall.issued_at + timedelta(minutes=30))
            else "pending",
            "Auditoría local del reporte; comparar verdad/diff en el portal",
            recall_id=identity,
            report=recall.last_report if recall else None,
        )
    return checks


async def run(args):
    settings = get_settings()
    require_development(settings, args.environment)
    report = {
        "environment": "dev",
        "scope": "e3_read_only_observation",
        "started_at": datetime.now(UTC).isoformat(),
        "period_id": str(args.period_id) if args.period_id else None,
        "llm_calls": 0,
        "business_mutations": 0,
        "step24_status": "pending",
        "checks": {},
        "not_verified": [
            "Verdad/diff del portal y recepción visual del chat",
            "Reinicio real con IDs y timestamps antes/después",
            "Cobertura completa de canales, mercado, consultas y recalls propio/ajeno",
            "Semántica remota de idempotencia de consultas y texto",
        ],
    }
    engine = create_engine(
        settings.database_url, poolclass=NullPool, connect_args={"connect_timeout": 3}
    )
    try:
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout='5000ms'"))
            if args.list_ids:
                periods = session.scalars(
                    select(BusinessPeriod)
                    .where(
                        BusinessPeriod.group_id == settings.farma_central_group,
                        BusinessPeriod.environment == "dev",
                    )
                    .order_by(BusinessPeriod.starts_at.desc())
                    .limit(20)
                ).all()
                report["checks"]["discovery"] = result(
                    "pending",
                    "Selecciona un período e IDs explícitos",
                    periods=[
                        {
                            "period_id": str(p.id),
                            "code": p.code,
                            "starts_at": p.starts_at.isoformat(),
                        }
                        for p in periods
                    ],
                )
                if args.period_id is not None:
                    p = next((p for p in periods if p.id == args.period_id), None)
                    if p is None:
                        raise ValueError("Selected period not in development group")
                    report["checks"]["discovery"]["trade_ids"] = [
                        str(value)
                        for value in session.scalars(
                            select(MarketTrade.id)
                            .where(MarketTrade.period_id == p.id)
                            .limit(20)
                        )
                    ]
                    report["checks"]["discovery"]["order_ids"] = list(
                        session.scalars(
                            select(Order.external_order_id)
                            .where(
                                Order.external_order_id.is_not(None),
                                Order.created_at >= p.starts_at,
                                Order.created_at < p.ends_at if p.ends_at else True,
                            )
                            .limit(20)
                        )
                    )
                    report["checks"]["discovery"]["message_ids"] = list(
                        session.scalars(
                            select(MessagingMessage.external_message_id)
                            .where(
                                MessagingMessage.period_id == p.id,
                                MessagingMessage.direction == MessageDirection.INBOUND,
                            )
                            .limit(20)
                        )
                    )
                    report["checks"]["discovery"]["recall_ids"] = list(
                        session.scalars(
                            select(Recall.recall_id)
                            .where(
                                Recall.issued_at >= p.starts_at,
                                Recall.issued_at < p.ends_at if p.ends_at else True,
                            )
                            .limit(20)
                        )
                    )
                report.update(
                    automated_status="pending",
                    finished_at=datetime.now(UTC).isoformat(),
                )
                return report
            period = session.get(BusinessPeriod, args.period_id)
            if (
                period is None
                or period.group_id != settings.farma_central_group
                or period.environment != "dev"
            ):
                raise ValueError("Period does not match development group")
            async with AsyncExitStack() as stack:
                orders = await stack.enter_async_context(
                    PurchaseOrdersClient(settings=settings)
                )
                billing = await stack.enter_async_context(
                    BillingClient(settings=settings)
                )
                trades = await stack.enter_async_context(
                    TradesClient(settings=settings)
                )
                report["checks"] = await inspect(
                    session, period, args, settings, orders, billing, trades
                )
    except Exception as exc:
        report["checks"]["observation"] = result(
            "pending",
            "No se pudo completar observación; no se reintentan mutaciones",
            error_type=type(exc).__name__,
        )
    finally:
        engine.dispose()
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["automated_status"] = (
        "failed"
        if any(item["status"] == "failed" for item in report["checks"].values())
        else "pending"
        if len(report["checks"]) <= 1
        or any(item["status"] == "pending" for item in report["checks"].values())
        else "passed"
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=["dev"])
    parser.add_argument("--period-id", type=UUID)
    parser.add_argument("--list-ids", action="store_true")
    parser.add_argument("--order-id", action="append", default=[])
    parser.add_argument("--trade-id", action="append", type=UUID, default=[])
    parser.add_argument("--message-id", action="append", default=[])
    parser.add_argument("--recall-id", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.period_id is None and not args.list_ids:
        parser.error("Select --period-id or use --list-ids first")
    if args.output.exists():
        parser.error("Output already exists; choose a new evidence file")
    try:
        report = asyncio.run(run(args))
    except ValueError:
        parser.error("Development configuration required; no requests sent")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "automated_status": report["automated_status"],
                "step24_status": report["step24_status"],
                "output": str(args.output),
            }
        )
    )
    return 1 if report["automated_status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
