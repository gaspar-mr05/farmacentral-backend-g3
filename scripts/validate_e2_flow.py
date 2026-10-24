"""Prepare and observe a real development order/recall flow without using the LLM."""

import argparse
import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    Order,
    OrderSource,
    OrderStatus,
    ProductionRun,
    Recall,
    RecallNotificationStatus,
    RecallStatus,
)
from app.queries.recalls import affected_lineage
from app.schemas.purchase_orders import PurchaseOrderState
from app.schemas.recalls import RecallReportRequest
from app.services.production.kits import KitProductionService
from app.services.production.lineage import has_official_lineage
from scripts.audit_lot_identities import audit
from scripts.empty_packaging import empty_packaging
from scripts.verify_e1_traceability import verify_delivery

logger = logging.getLogger(__name__)
DEV_HOST = "dev.proyecto.2026-2.tallerdeintegracion.cl"


class ValidationSetupError(ValueError):
    """A local, safe-to-display validation diagnostic."""


def result(status: str, detail: str, **evidence) -> dict:
    return {"status": status, "detail": detail, **evidence}


def require_development(settings) -> None:
    for url in (
        str(settings.farma_central_base_url),
        settings.effective_purchase_orders_base_url,
    ):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != DEV_HOST:
            raise ValidationSetupError(
                "Este script solo admite las APIs oficiales de desarrollo"
            )


def completed_quantities(session: Session) -> dict[str, int]:
    return dict(
        session.execute(
            select(
                ProductionRun.expected_sku, func.sum(ProductionRun.expected_quantity)
            )
            .where(
                ProductionRun.completed_at.is_not(None),
                ProductionRun.output_lot_id.is_not(None),
            )
            .group_by(ProductionRun.expected_sku)
        ).all()
    )


def database_check(session: Session) -> dict:
    migrations = Path(__file__).resolve().parents[1] / "alembic"
    expected = set(ScriptDirectory(str(migrations)).get_heads())
    current = set(MigrationContext.configure(session.connection()).get_current_heads())
    return result(
        "passed" if current == expected else "failed",
        "Migraciones al día" if current == expected else "Ejecuta alembic upgrade head",
        current=sorted(current),
        expected=sorted(expected),
    )


async def check_order(session: Session, order_id: str, client, group: int) -> dict:
    order = session.scalar(select(Order).where(Order.external_order_id == order_id))
    if order is None:
        return result("pending", "Esperando ingreso de la OC por sFTP o Pub/Sub")
    if order.source not in {OrderSource.SFTP, OrderSource.PUBSUB}:
        return result(
            "failed", "La OC de esta validación debe ingresar por sFTP/Pub/Sub"
        )
    if order.status in {
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.CANCELLED,
    }:
        return result("failed", f"La OC terminó como {order.status.value}")
    if order.status != OrderStatus.DISPATCHED:
        return result("pending", f"OC local: {order.status.value}")

    deliveries = []
    for item in order.items:
        if len(item.assigned_units) != item.quantity:
            return result(
                "failed", "La cantidad despachada local no coincide con la OC"
            )
        for assignment in item.assigned_units:
            if (
                assignment.dispatched_at is None
                or order.delivery_deadline is None
                or assignment.dispatched_at > order.delivery_deadline
            ):
                return result("failed", "Falta despacho dentro del plazo")
            if not has_official_lineage(assignment.unit.lot):
                return result(
                    "failed", "La entrega tiene lotes o consumos sin identificar"
                )
            try:
                deliveries.append(verify_delivery(session, assignment.external_unit_id))
            except ValueError as exc:
                return result("failed", str(exc))
    if not deliveries:
        return result("failed", "La OC no tiene entregas verificables")

    official = await client.get_order(order_id)
    if (
        official.supplier_id != str(group)
        or official.customer_id != order.external_customer_id
        or len(order.items) != 1
        or order.items[0].sku != official.sku
        or order.items[0].quantity != official.quantity
    ):
        return result("failed", "Los datos locales no coinciden con la OC oficial")
    if (
        official.state != PurchaseOrderState.FULFILLED
        or official.dispatched_quantity != official.quantity
    ):
        return result("pending", "Falta confirmación oficial de cumplimiento completo")
    if any(
        assignment.dispatched_at > official.delivery_deadline
        for item in order.items
        for assignment in item.assigned_units
    ):
        return result("failed", "El despacho supera el vencimiento oficial")
    return result(
        "passed",
        "OC cumplida y trazabilidad reconstruida",
        channel=order.source.value,
        deliveries=deliveries,
    )


async def check_recall(session: Session, recall_id: str, client) -> dict:
    event = session.scalar(
        select(InboundEvent).where(
            InboundEvent.source == InboundEventSource.RECALL,
            InboundEvent.external_event_id == recall_id,
        )
    )
    recall = session.scalar(select(Recall).where(Recall.recall_id == recall_id))
    if recall is None or recall.status != RecallStatus.COMPLETED:
        if event is not None and event.status == InboundEventStatus.FAILED:
            return result("failed", event.last_error or "Evento de recall fallido")
        if recall is not None and recall.deadline <= datetime.now(UTC):
            return result("failed", "El recall venció sin completarse")
        return result(
            "pending", "Esperando recepción y respuesta del recall por el worker"
        )
    if (
        recall.reported_at is None
        or recall.reported_at > recall.deadline
        or recall.origin_lot is None
        or recall.origin_lot.external_lot_id != recall.batch
        or recall.origin_lot.product.sku != recall.sku
    ):
        return result(
            "failed", "El origen o el reporte dentro del plazo no son verificables"
        )
    if not all(has_official_lineage(item.lot) for item in recall.lots):
        return result("failed", "El recall contiene lotes con trazabilidad incompleta")
    reachable = {row.lot_id for row in affected_lineage(session, recall.origin_lot_id)}
    if reachable != {item.lot_id for item in recall.lots}:
        return result("failed", "La investigación no cubre todos los lotes derivados")
    report = RecallReportRequest.model_validate(recall.last_report)
    snapshots = [
        item
        for item in recall.units
        if item.unit.status not in {"consumed", "dispatched"}
    ]
    expected_stock = {
        (
            item.unit.external_unit_id,
            item.unit.lot.external_lot_id,
            item.original_location.code,
        )
        for item in snapshots
    }
    actual_stock = {
        (item.product_id, item.batch, item.store) for item in report.in_stock
    }
    lots = {item.id: item for item in recall.lots}
    expected_derived = {
        (
            item.lot.external_lot_id,
            item.lot.product.sku,
            lots[item.parent_id].lot.external_lot_id,
        )
        for item in recall.lots
        if item.parent_id is not None
    }
    actual_derived = {
        (item.batch, item.sku, item.source_batch) for item in report.derived_batches
    }
    expected_deliveries = {
        (
            item.order.external_order_id or str(item.order_id),
            item.order.buyer_name,
            tuple(
                sorted(delivery.unit.external_unit_id for delivery in item.deliveries)
            ),
        )
        for item in recall.orders
    }
    actual_deliveries = {
        (item.order_id, item.customer_name, tuple(sorted(item.product_ids)))
        for item in report.deliveries
    }
    if (
        expected_stock != actual_stock
        or len(actual_stock) != len(report.in_stock)
        or expected_derived != actual_derived
        or len(actual_derived) != len(report.derived_batches)
        or expected_deliveries != actual_deliveries
        or len(actual_deliveries) != len(report.deliveries)
    ):
        return result(
            "failed", "El reporte no coincide con la investigación persistida"
        )
    for affected in recall.orders:
        notification = affected.notification
        if (
            notification is None
            or notification.status != RecallNotificationStatus.SENT
            or notification.sent_at is None
            or notification.sent_at > recall.deadline
            or notification.payload.get("orderId")
            != (affected.order.external_order_id or str(affected.order_id))
            or set(notification.payload.get("productIds", []))
            != {item.unit.external_unit_id for item in affected.deliveries}
        ):
            return result("failed", "Falta una notificación válida dentro del plazo")
    spaces = await client.get_spaces()
    quarantine_ids = {space["_id"] for space in spaces if space.get("quarantine")}
    for snapshot in snapshots:
        if (
            snapshot.quarantined_at is None
            or snapshot.quarantined_at > recall.deadline
            or snapshot.unit.status != "quarantined"
            or snapshot.unit.current_location.code not in quarantine_ids
        ):
            return result("failed", "Hay stock afectado sin cuarentena confirmada")
    # Read official stock; a truncated response cannot prove a missing unit is absent.
    for store in quarantine_ids:
        for sku in {item.unit.lot.product.sku for item in snapshots}:
            expected = {
                item.unit.external_unit_id
                for item in snapshots
                if item.unit.current_location.code == store
                and item.unit.lot.product.sku == sku
            }
            observed = {
                item["_id"]
                for item in await client.get_space_products(store, sku, limit=200)
            }
            if not expected <= observed:
                return result(
                    "pending", "La consulta oficial no confirma toda la cuarentena"
                )
    if (
        not expected_stock
        or not expected_deliveries
        or not any(item.generation >= 2 for item in recall.lots)
    ):
        return result(
            "pending",
            "Falta cubrir stock, entregas y cadena insumo→acondicionado→kit",
            stock=len(expected_stock),
            orders=len(expected_deliveries),
            derived_batches=len(expected_derived),
        )
    return result(
        "passed",
        "Cuarentena oficial y auditoría local verificadas; comparar con el portal",
        batch=recall.batch,
        stock=len(expected_stock),
        orders=len(expected_deliveries),
        derived_batches=len(expected_derived),
        reported_at=recall.reported_at.isoformat(),
        report=recall.last_report,
    )


def check_flow(checks: dict, order_ids: list[str], recall_id: str | None) -> dict:
    if not order_ids or not recall_id:
        return result("pending", "Faltan IDs de OC y recall para vincular el flujo")
    orders = [checks[f"order:{order_id}"] for order_id in order_ids]
    recall = checks["recall"]
    if any(item["status"] != "passed" for item in [*orders, recall]):
        return result("pending", "Primero deben verificarse las OCs y el recall")
    reported_orders = {item["orderId"] for item in recall["report"]["deliveries"]}
    if not set(order_ids) <= reported_orders:
        return result("failed", "El recall no incluye las OCs indicadas")
    if {item["channel"] for item in orders} != {"sftp", "pubsub"}:
        return result("pending", "Falta una OC cumplida por cada canal: sFTP y Pub/Sub")
    return result(
        "passed", "Las entregas de ambos canales están incluidas en el recall"
    )


async def prepare(args) -> dict:
    with SessionLocal() as session:
        if database_check(session)["status"] != "passed":
            raise ValidationSetupError("Aplica las migraciones antes de preparar stock")
        async with FarmaCentralClient() as client:
            service = KitProductionService(
                client,
                session,
                raw_material_source="supply",
                require_official_batches=True,
            )
            runs = await service.produce_kits(
                skus=[args.sku], quantity_per_sku=args.target
            )
            output_runs = runs[args.sku]
            if any(
                run.output_lot is None or not has_official_lineage(run.output_lot)
                for run in output_runs
            ):
                raise ValidationSetupError(
                    "La producción aún no tiene batches oficiales completos"
                )
            moved = await empty_packaging(client, session)
            return {
                "runs": [str(run.id) for run in output_runs],
                "moved_to_buffer": moved,
            }


async def inspect(args) -> dict:
    checks = {}
    with SessionLocal() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        checks["database"] = database_check(session)
        if checks["database"]["status"] != "passed":
            return {"checks": checks}
        rows = audit(session)
        inventory = {
            "unresolved_historical_units": sum(row["units"] for row in rows),
            "completed_quantities": completed_quantities(session),
        }
        if args.order_id:
            async with PurchaseOrdersClient() as client:
                for order_id in args.order_id:
                    checks[f"order:{order_id}"] = await check_order(
                        session,
                        order_id,
                        client,
                        get_settings().farma_central_group,
                    )
        else:
            checks["order"] = result(
                "pending", "Falta --order-id de una OC real del grupo"
            )
        if args.recall_id:
            async with FarmaCentralClient() as client:
                checks["recall"] = await check_recall(session, args.recall_id, client)
        else:
            checks["recall"] = result(
                "pending", "Genera el retiro en el portal y usa --recall-id"
            )
        checks["flow"] = check_flow(checks, args.order_id or [], args.recall_id)
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
        response = await client.get(f"{args.api_url.rstrip('/')}/api/health")
        checks["health"] = result(
            "passed" if response.status_code == 200 else "failed",
            f"API health: HTTP {response.status_code}",
        )
    return {"checks": checks, "inventory": inventory}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Compra, produce y mueve stock real en dev",
    )
    parser.add_argument("--sku", help="Kit que se preparará")
    parser.add_argument(
        "--target",
        type=int,
        help="Meta acumulada de producción del SKU, no stock actual",
    )
    parser.add_argument(
        "--order-id", action="append", help="OC real; repetir para sFTP y Pub/Sub"
    )
    parser.add_argument(
        "--recall-id", help="Retiro real generado en el portal de desarrollo"
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=0,
        help="Tiempo máximo de observación de workers",
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--output", type=Path, help="Guardar evidencia JSON, por ejemplo /tmp/e2.json"
    )
    args = parser.parse_args()
    if args.prepare and (not args.sku or args.target is None or args.target <= 0):
        parser.error("--prepare requiere --sku y --target positivo")
    if not args.prepare and (args.sku or args.target is not None):
        parser.error("--sku y --target requieren --prepare")
    if args.wait_seconds < 0:
        parser.error("--wait-seconds no puede ser negativo")
    if any(not re.fullmatch(r"[0-9a-f]{24}", value) for value in args.order_id or []):
        parser.error("--order-id debe tener 24 caracteres hexadecimales en minúsculas")
    if args.output and args.output.exists():
        parser.error("--output ya existe; usa otro archivo para esta ejecución")
    return args


async def main() -> int:
    args = arguments()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    evidence = {
        "started_at": datetime.now(UTC).isoformat(),
        "llm_calls": 0,
        "not_verified": [
            "Mensajería real con LLM",
            "Comparación con solución esperada del portal",
            "Disponibilidad continua durante toda la evaluación",
        ],
    }
    try:
        require_development(get_settings())
        if args.prepare:
            logger.info("Preparando %s hasta meta acumulada %s", args.sku, args.target)
            evidence["preparation"] = await prepare(args)
        deadline = asyncio.get_running_loop().time() + args.wait_seconds
        while True:
            evidence.update(await inspect(args))
            states = {item["status"] for item in evidence["checks"].values()}
            for name, item in evidence["checks"].items():
                logger.info("%s: %s — %s", name, item["status"], item["detail"])
            remaining = deadline - asyncio.get_running_loop().time()
            if "failed" in states or "pending" not in states or remaining <= 0:
                break
            await asyncio.sleep(min(10, remaining))
        code = 1 if "failed" in states else 2 if "pending" in states else 0
    except ValidationSetupError as exc:
        evidence["error"] = str(exc)
        logger.error("%s", exc)
        code = 1
    except Exception as exc:
        # Exception bodies from external APIs can contain private data.
        evidence["error"] = type(exc).__name__
        logger.error(
            "Validación interrumpida (%s); revisa configuración y servicios",
            type(exc).__name__,
        )
        code = 1
    evidence["finished_at"] = datetime.now(UTC).isoformat()
    evidence["exit_code"] = code
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        # Never overwrite a user's env/config file with evidence.
        with args.output.open("x", encoding="utf-8") as output:
            output.write(encoded + "\n")
    print(encoded)
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
