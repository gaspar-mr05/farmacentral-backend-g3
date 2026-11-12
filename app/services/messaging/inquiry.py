import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.purchase_orders import PurchaseOrdersClient, PurchaseOrdersHTTPError
from app.models import (
    InboundEvent,
    MessageDirection,
    MessagingDecision,
    MessagingMessage,
)
from app.schemas.purchase_orders import PurchaseOrder

CHILE = ZoneInfo("America/Santiago")
ORDER_ID = re.compile(r"(?<![0-9a-f])([0-9a-f]{24})(?![0-9a-f])", re.IGNORECASE)
NUMBER_WORDS = {
    "un": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
}
UNRESOLVED_RESPONSE = (
    "No pude identificar una única OC válida para esta consulta. "
    "Revisa el ID de la OC o describe el pedido con más detalle."
)


@dataclass(frozen=True)
class OrderCandidate:
    order_id: str
    sku: str
    quantity: int
    occurred_at: datetime


class InquiryResolutionService:
    """Resolve explicit IDs or natural references to prior conversation orders."""

    def __init__(
        self,
        client: PurchaseOrdersClient,
        session: Session,
        *,
        group_id: int,
    ) -> None:
        self._client = client
        self._session = session
        self._group_id = group_id

    async def resolve(self, event: InboundEvent, *, now: datetime | None = None) -> str:
        if event.decision is not MessagingDecision.INQUIRY:
            raise ValueError("Only inquiry events can be resolved")

        current = self._inbound_message(event)
        if current is None:
            return self._unresolved(event, "missing_conversation", [])

        text = str(event.payload.get("text", ""))
        cited_ids = _cited_order_ids(text)
        candidates: list[OrderCandidate] = []
        if cited_ids:
            if len(cited_ids) != 1:
                return self._unresolved(
                    event,
                    "ambiguous",
                    candidates,
                    criteria=["order_id"],
                )
            selected_order_id = cited_ids.pop()
            method = "cited_order_id"
            criteria = ["order_id"]
        else:
            candidates = self._candidates(current)
            selected, method, criteria = _select_candidate(
                text, current.occurred_at, candidates
            )
            if selected is None:
                status = "ambiguous" if len(candidates) > 1 else "unresolved"
                return self._unresolved(
                    event,
                    status,
                    candidates,
                    criteria=criteria,
                )
            selected_order_id = selected.order_id

        try:
            order = await self._client.get_order(selected_order_id)
        except PurchaseOrdersHTTPError as exc:
            if exc.status_code != 404:
                raise
            return self._unresolved(
                event,
                "order_not_found",
                candidates,
                criteria=criteria,
            )
        if order.supplier_id != str(self._group_id):
            return self._unresolved(
                event,
                "supplier_mismatch",
                candidates,
                criteria=criteria,
            )

        observed_at = now or datetime.now(UTC)
        event.consulted_external_order_id = order.id.lower()
        event.order_status_snapshot = {
            "observedAt": observed_at.isoformat(),
            **order.model_dump(mode="json", by_alias=True),
        }
        event.resolution_evidence = {
            "status": "resolved",
            "method": method,
            "criteria": criteria,
            "candidateOrderIds": [candidate.order_id for candidate in candidates],
            "selectedOrderId": selected_order_id,
            "responseText": _response_text(order),
        }
        self._session.flush()
        return event.resolution_evidence["responseText"]

    def _inbound_message(self, event: InboundEvent) -> MessagingMessage | None:
        return self._session.scalar(
            select(MessagingMessage).where(
                MessagingMessage.inbound_event_id == event.id,
                MessagingMessage.direction == MessageDirection.INBOUND,
            )
        )

    def _candidates(self, current: MessagingMessage) -> list[OrderCandidate]:
        period_filter = (
            MessagingMessage.period_id.is_(None)
            if current.period_id is None
            else MessagingMessage.period_id == current.period_id
        )
        rows = self._session.execute(
            select(InboundEvent, MessagingMessage)
            .join(
                MessagingMessage,
                MessagingMessage.inbound_event_id == InboundEvent.id,
            )
            .where(
                MessagingMessage.direction == MessageDirection.INBOUND,
                MessagingMessage.phone_normalized == current.phone_normalized,
                MessagingMessage.group_id == current.group_id,
                MessagingMessage.environment == current.environment,
                period_filter,
                MessagingMessage.occurred_at < current.occurred_at,
                InboundEvent.decision == MessagingDecision.ORDER,
                InboundEvent.external_order_id.is_not(None),
                InboundEvent.interpreted_sku.is_not(None),
                InboundEvent.interpreted_quantity.is_not(None),
            )
            .order_by(MessagingMessage.occurred_at)
        ).all()
        unique: dict[str, OrderCandidate] = {}
        for prior, message in rows:
            order_id = prior.external_order_id.lower()
            unique[order_id] = OrderCandidate(
                order_id=order_id,
                sku=prior.interpreted_sku,
                quantity=prior.interpreted_quantity,
                occurred_at=message.occurred_at,
            )
        return list(unique.values())

    def _unresolved(
        self,
        event: InboundEvent,
        status: str,
        candidates: list[OrderCandidate],
        *,
        criteria: list[str] | None = None,
    ) -> str:
        event.consulted_external_order_id = None
        event.order_status_snapshot = None
        event.resolution_evidence = {
            "status": status,
            "criteria": criteria or [],
            "candidateOrderIds": [candidate.order_id for candidate in candidates],
            "responseText": UNRESOLVED_RESPONSE,
        }
        self._session.flush()
        return UNRESOLVED_RESPONSE


def _select_candidate(
    text: str,
    received_at: datetime,
    candidates: list[OrderCandidate],
) -> tuple[OrderCandidate | None, str | None, list[str]]:
    normalized = _normalize(text)
    remaining = list(candidates)
    criteria: list[str] = []

    sku_matches = [
        candidate for candidate in remaining if _mentions_sku(normalized, candidate.sku)
    ]
    if sku_matches:
        remaining = sku_matches
        criteria.append("sku")

    quantities = _mentioned_quantities(normalized)
    if quantities:
        remaining = [
            candidate for candidate in remaining if candidate.quantity in quantities
        ]
        criteria.append("quantity")

    temporal = _temporal_filter(normalized, received_at, remaining)
    if temporal is not None:
        remaining = temporal
        criteria.append("time")

    if len(remaining) == 1:
        method = "single_candidate" if len(candidates) == 1 else "description"
        return remaining[0], method, criteria
    return None, None, criteria


def _cited_order_ids(text: str) -> set[str]:
    return {value.lower() for value in ORDER_ID.findall(text)}


def _normalize(value: str) -> str:
    value = "".join(
        char
        for char in unicodedata.normalize("NFD", value.casefold())
        if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _mentions_sku(text: str, sku: str) -> bool:
    normalized_sku = _normalize(sku)
    if normalized_sku in text:
        return True
    tokens = {
        token for token in normalized_sku.split() if len(token) >= 4 and token != "kit"
    }
    return bool(tokens) and tokens.issubset(set(text.split()))


def _mentioned_quantities(text: str) -> set[int]:
    values: set[int] = set()
    pattern = r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(?:kits?|unidades?)\b"
    for value in re.findall(pattern, text):
        values.add(int(value) if value.isdecimal() else NUMBER_WORDS[value])
    if re.search(r"\b(?:un par|dos)\s+de\s+kits?\b", text):
        values.add(2)
    return values


def _temporal_filter(
    text: str,
    received_at: datetime,
    candidates: list[OrderCandidate],
) -> list[OrderCandidate] | None:
    local_received = received_at.astimezone(CHILE)
    if re.search(r"\bayer\b", text):
        day = local_received.date() - timedelta(days=1)
        return [
            candidate
            for candidate in candidates
            if candidate.occurred_at.astimezone(CHILE).date() == day
        ]
    if re.search(r"\bhoy\b", text):
        day = local_received.date()
        return [
            candidate
            for candidate in candidates
            if candidate.occurred_at.astimezone(CHILE).date() == day
        ]
    if re.search(r"\b(?:en|por|de) la manana\b|\besta manana\b", text):
        return [
            candidate
            for candidate in candidates
            if candidate.occurred_at.astimezone(CHILE).hour < 12
        ]
    if re.search(r"\b(?:en|por|de) la tarde\b|\besta tarde\b", text):
        return [
            candidate
            for candidate in candidates
            if 12 <= candidate.occurred_at.astimezone(CHILE).hour < 20
        ]
    if re.search(r"\b(?:en|por|de) la noche\b|\besta noche\b", text):
        return [
            candidate
            for candidate in candidates
            if candidate.occurred_at.astimezone(CHILE).hour >= 20
        ]
    if re.search(r"\b(?:ultimo|ultima|mas reciente)\b", text) and candidates:
        return [max(candidates, key=lambda candidate: candidate.occurred_at)]
    if re.search(r"\b(?:primero|primera)\b", text) and candidates:
        return [min(candidates, key=lambda candidate: candidate.occurred_at)]
    return None


def _response_text(order: PurchaseOrder) -> str:
    return (
        f"La OC {order.id.lower()} está {order.state.value}. "
        f"Se han despachado {order.dispatched_quantity} de {order.quantity} "
        f"unidades de {order.sku}. Vence el "
        f"{order.delivery_deadline.astimezone(CHILE).isoformat()}."
    )
