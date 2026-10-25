"""Read-only checks; never interpret messages or run background workers."""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
    Order,
    OrderSource,
    OrderStatus,
    Product,
    ProductCategory,
)
from app.schemas.messaging import MessagingOrderInterpretation, MessagingWebhookPayload
from app.schemas.purchase_orders import PurchaseOrderState
from app.schemas.recalls import RecallDetailResponse, RecallSummaryResponse
from app.services.recalls.query import RecallNotFoundError, RecallQueryService

WORKERS = (
    "poll_sftp_orders",
    "poll_pubsub_orders",
    "process_messaging",
    "fulfill_external_orders",
    "process_recalls",
)


def result(status: str, detail: str, **evidence) -> dict:
    return {"status": status, "detail": detail, **evidence}


def matches_official_order(order: Order, official, group: int) -> bool:
    return (
        official.supplier_id == str(group)
        and official.customer_id == order.external_customer_id
        and len(order.items) == 1
        and order.items[0].sku == official.sku
        and order.items[0].quantity == official.quantity
        and order.delivery_deadline == official.delivery_deadline
    )


async def check_message(session: Session, message_id: str, client, group: int) -> dict:
    event = session.scalar(
        select(InboundEvent).where(
            InboundEvent.source == InboundEventSource.MESSAGING,
            InboundEvent.external_event_id == message_id,
        )
    )
    if event is None:
        return result("pending", "Esperando recepción del mensaje")
    payload = MessagingWebhookPayload.model_validate(event.payload)
    deadline = payload.received_at + timedelta(minutes=30)
    if event.responded_at is None:
        if deadline <= datetime.now(UTC):
            return result("failed", "El mensaje venció sin respuesta confirmada")
        if event.status == InboundEventStatus.FAILED and event.next_retry_at is None:
            return result("failed", "El mensaje falló sin reintento programado")
        return result("pending", "Esperando al worker; este script no consume LLM")
    if not payload.received_at <= event.responded_at <= deadline:
        return result("failed", "La respuesta no está dentro del plazo de 30 minutos")
    if event.status != InboundEventStatus.PROCESSED:
        return result(
            "failed"
            if event.status == InboundEventStatus.FAILED and event.next_retry_at is None
            else "pending",
            "La respuesta está registrada pero el procesamiento no terminó",
        )
    evidence = {
        "decision": event.decision.value if event.decision else None,
        "responded_at": event.responded_at.isoformat(),
        "llm_output_recorded": event.llm_output is not None,
    }
    if event.decision == MessagingDecision.DISCARDED:
        if (
            not event.reason
            or not event.reason.strip()
            or event.external_order_id
            or event.order_id
        ):
            return result(
                "failed", "El descarte carece de razón o está vinculado a una OC"
            )
        return result("passed", "Descarte auditado dentro del plazo", **evidence)
    if event.decision != MessagingDecision.ORDER:
        return result("failed", "Falta una decisión persistida")
    interpretation = MessagingOrderInterpretation(
        sku=event.interpreted_sku,
        quantity=event.interpreted_quantity,
        deliveryDate=event.interpreted_delivery_date,
    )
    product = session.scalar(select(Product).where(Product.sku == interpretation.sku))
    if (
        product is None
        or product.category != ProductCategory.KIT
        or interpretation.delivery_date <= payload.received_at
    ):
        return result(
            "failed", "La interpretación no identifica un kit y plazo válidos"
        )
    order = session.get(Order, event.order_id) if event.order_id else None
    if order is None:
        return result("failed", "Falta la OC local vinculada al mensaje procesado")
    if (
        order.source != OrderSource.MESSAGING
        or order.external_order_id != event.external_order_id
        or len(order.items) != 1
        or order.items[0].sku != interpretation.sku
        or order.items[0].quantity != interpretation.quantity
        or order.delivery_deadline != interpretation.delivery_date
        or order.buyer_name != payload.sender.name
    ):
        return result("failed", "La OC local no coincide con el mensaje interpretado")
    if (
        order.status == OrderStatus.RECEIVED
        or order.decision_at is None
        or not order.decision_reason
    ):
        return result(
            "pending", "La OC del mensaje todavía no tiene una decisión auditada"
        )
    official = await client.get_order(order.external_order_id)
    if not matches_official_order(order, official, group):
        return result(
            "failed", "La OC de mensajería no coincide con el servicio oficial"
        )
    if official.state == PurchaseOrderState.CREATED:
        return result("pending", "Falta la decisión en el servicio oficial de OCs")
    if (
        order.status == OrderStatus.REJECTED
        and official.state != PurchaseOrderState.REJECTED
    ):
        return result("failed", "El rechazo local no coincide con la OC oficial")
    if (
        order.status in {OrderStatus.ACCEPTED, OrderStatus.DISPATCHED}
        and official.state == PurchaseOrderState.REJECTED
    ):
        return result("failed", "La OC oficial rechazada contradice la decisión local")
    return result(
        "passed",
        "Mensaje vinculado a su OC y decisión oficial; revisar su interpretación",
        order_id=order.external_order_id,
        order_status=order.status.value,
        **evidence,
    )


async def check_recall_api(
    session: Session, recall_id: str, client, api_url: str
) -> dict:
    try:
        expected = RecallQueryService(session).get_recall(recall_id)
    except RecallNotFoundError:
        return result("pending", "El recall aún no está disponible para la API pública")
    base = api_url.rstrip("/")
    listing = await client.get(f"{base}/api/recalls")
    detail = await client.get(f"{base}/api/recalls/{quote(recall_id, safe='')}")
    if listing.status_code != 200 or detail.status_code != 200:
        return result(
            "pending"
            if any(
                code >= 500 or code == 429
                for code in (listing.status_code, detail.status_code)
            )
            else "failed",
            "La API pública de recalls no responde HTTP 200",
            list_http=listing.status_code,
            detail_http=detail.status_code,
        )
    body = listing.json()
    if not isinstance(body, list):
        return result("failed", "El listado público no es una lista de recalls")
    summaries = [RecallSummaryResponse.model_validate(item) for item in body]
    observed = RecallDetailResponse.model_validate(detail.json())
    matches = [item for item in summaries if item.recall_id == recall_id]
    if len(matches) != 1 or observed.recall_id != recall_id:
        return result(
            "failed", "El recall no aparece exactamente una vez en listado y detalle"
        )
    if observed.batch != expected.batch or observed.sku != expected.sku:
        return result(
            "failed", "La API pública devuelve un lote o SKU distinto de la DB"
        )
    actual_units = {(unit.product_id, unit.sku, unit.batch) for unit in observed.units}
    expected_units = {
        (unit.product_id, unit.sku, unit.batch) for unit in expected.units
    }
    if actual_units != expected_units or len(actual_units) != len(observed.units):
        return result(
            "failed",
            "Las unidades públicas no coinciden con la investigación persistida",
        )
    if observed.status != expected.status or matches[
        0
    ].model_dump() != observed.model_dump(exclude={"units"}):
        return result(
            "pending", "El estado público cambió durante la consulta; volver a observar"
        )
    return result(
        "passed",
        "Listado y detalle públicos coinciden con la DB",
        units=len(actual_units),
    )


async def check_workers(*, include_replenishment: bool = False) -> dict:
    names = (*WORKERS, "replenish_inventory") if include_replenishment else WORKERS
    units = [f"farmacentral-worker@{name}.service" for name in names]
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "show",
            "--no-pager",
            "--property=Id,LoadState,ActiveState,SubState,Restart",
            *units,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return result(
            "pending",
            "systemctl no está disponible; ejecutar esta comprobación en el servidor",
        )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return result("failed", "systemctl no respondió dentro de 5 segundos")
    if process.returncode != 0:
        return result(
            "pending",
            "No se pudo consultar systemd en este equipo",
            command_exit=process.returncode,
        )
    observed = {}
    for block in stdout.decode(errors="replace").strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Id") in units:
            observed[fields.pop("Id")] = fields
    invalid = [
        unit
        for unit in units
        if (
            observed.get(unit, {}).get("LoadState") != "loaded"
            or observed[unit].get("ActiveState") != "active"
            or observed[unit].get("SubState") != "running"
            or observed[unit].get("Restart") not in {"always", "on-failure"}
        )
    ]
    return result(
        "failed" if invalid else "passed",
        "Hay workers ausentes, detenidos o sin reinicio automático"
        if invalid
        else "Workers activos con reinicio; disponibilidad continua pendiente",
        units=observed,
        invalid=invalid,
    )


def check_step19(checks: dict) -> dict:
    required = {
        "database": checks.get("database", {}).get("status") == "passed",
        "api_health": checks.get("health", {}).get("status") == "passed",
        "sftp": any(
            item.get("channel") == "sftp"
            and item.get("fulfilled")
            and item["status"] == "passed"
            for item in checks.values()
        ),
        "pubsub": any(
            item.get("channel") == "pubsub"
            and item.get("fulfilled")
            and item["status"] == "passed"
            for item in checks.values()
        ),
        "messaging": any(
            name.startswith("message:") and item["status"] == "passed"
            for name, item in checks.items()
        ),
        "recall": checks.get("recall", {}).get("status") == "passed",
        "recall_api": checks.get("recall_api", {}).get("status") == "passed",
        "workers": checks.get("workers", {}).get("status") == "passed",
    }
    missing = [name for name, passed in required.items() if not passed]
    return result(
        "pending" if missing else "passed",
        "Faltan evidencias operativas"
        if missing
        else "Checklist operativo cubierto; faltan las comprobaciones manuales",
        missing=missing,
        covered=[name for name in required if name not in missing],
    )
