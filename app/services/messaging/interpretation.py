import re
import unicodedata
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.llm_gateway import (
    LLMGatewayBudgetExceededError,
    LLMGatewayClient,
    LLMGatewayError,
    LLMGatewayInvalidResponseError,
    LLMGatewayResult,
    LLMGatewayTimeoutError,
)
from app.models import (
    InboundEvent,
    InboundEventSource,
    MessagingDecision,
    Product,
    ProductCategory,
)
from app.schemas.llm_gateway import LLMOrderProposal
from app.schemas.messaging import MessagingWebhookPayload

from .catalog import kit_catalog

CHILE = ZoneInfo("America/Santiago")
MIN_CONFIDENCE = 0.8
NUMBER_WORDS = (
    "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce "
    "trece catorce quince dieciseis diecisiete dieciocho diecinueve veinte"
)
NUMBERS = {word: value for value, word in enumerate(NUMBER_WORDS.split())} | {
    "un": 1,
    "una": 1,
    "un par": 2,
    "media docena": 6,
    "una docena": 12,
}
NUMBER = r"(?:\d+|" + "|".join(sorted(NUMBERS, key=len, reverse=True)) + r")"


class MessageInterpretationService:
    """Audit what the customer requested; accepting an OC is a separate step."""

    def __init__(
        self,
        client: LLMGatewayClient,
        session: Session,
        *,
        catalog_client: FarmaCentralClient,
    ) -> None:
        self._client = client
        self._session = session
        self._catalog_client = catalog_client

    async def propose(self, message: MessagingWebhookPayload) -> LLMGatewayResult:
        clinical_skus = set(
            self._session.scalars(
                select(Product.sku).where(Product.category == ProductCategory.KIT)
            )
        )
        catalog = kit_catalog(
            await self._catalog_client.get_available_products(),
            clinical_skus,
            message_text=message.text,
        )
        return await self._client.propose_order(
            text=message.text,
            received_at=message.received_at,
            catalog=catalog,
        )

    async def interpret(
        self, event: InboundEvent, *, now: datetime | None = None
    ) -> InboundEvent:
        if event.source != InboundEventSource.MESSAGING:
            raise ValueError("Only messaging events can be interpreted")
        if event.decision is not None:
            return event
        if event.responded_at is not None:
            raise ValueError("A responded message cannot be reinterpreted")
        if now is not None and now.utcoffset() is None:
            raise ValueError("now must include a timezone")

        proposal = None
        try:
            message = MessagingWebhookPayload.model_validate(event.payload)
        except ValidationError:
            reason = "El mensaje original no tiene un formato válido."
        else:
            try:
                result = await self.propose(message)
            except LLMGatewayError as exc:
                event.llm_output = (
                    exc.raw_output
                    if isinstance(exc, LLMGatewayInvalidResponseError)
                    else {
                        "error": type(exc).__name__,
                        "status_code": getattr(exc, "status_code", None),
                    }
                )
                if isinstance(exc, LLMGatewayBudgetExceededError):
                    reason = "Presupuesto del gateway agotado."
                elif isinstance(exc, LLMGatewayTimeoutError):
                    reason = "El modelo no respondió dentro del plazo."
                elif isinstance(exc, LLMGatewayInvalidResponseError):
                    reason = "La salida del modelo no tiene un formato válido."
                else:
                    reason = "No fue posible interpretar el mensaje con el gateway."
            else:
                event.llm_output = result.raw_output
                proposal = result.proposal
                kit_skus = set(
                    self._session.scalars(
                        select(Product.sku).where(
                            Product.category == ProductCategory.KIT
                        )
                    )
                )
                reason = _discard_reason(
                    message, proposal, kit_skus, now=now or datetime.now(UTC)
                )

        event.decision = (
            MessagingDecision.DISCARDED if reason else MessagingDecision.ORDER
        )
        event.reason = reason
        event.interpreted_sku = proposal.sku if not reason else None
        event.interpreted_quantity = proposal.quantity if not reason else None
        event.interpreted_delivery_date = (
            proposal.delivery_date.astimezone(UTC) if not reason else None
        )
        self._session.flush()
        return event


def _normalize(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text.casefold())
        if not unicodedata.combining(char)
    )


def _number(value: str) -> int:
    return int(value) if value.isdecimal() else NUMBERS[value]


def _discard_reason(
    message: MessagingWebhookPayload,
    proposal: LLMOrderProposal,
    kit_skus: set[str],
    *,
    now: datetime,
) -> str | None:
    text = _normalize(message.text)
    other_request = re.search(
        r"\b(ayuda|cancelar|cancela|cancelacion|despacharon)\b|"
        r"\bestado (?:de|del)\b|"
        r"\bno\s+(?:quiero|queremos|necesito|necesitamos|requiero|requerimos|"
        r"solicito|solicitamos|(?:nos|me)\s+(?:faltan?|hacen?\s+falta)|"
        r"(?:(?:nos|me)\s+)?(?:envies|envien|mandes|manden|"
        r"despaches|despachen|traigas|traigan))\b",
        text,
    )
    # Intent is classified by the model; a verb allowlist rejects valid paraphrases.
    if not proposal.is_order or other_request:
        return "El mensaje no identifica un pedido nuevo."
    if proposal.is_ambiguous or proposal.confidence < MIN_CONFIDENCE:
        return "La interpretación es ambigua o no tiene suficiente confianza."
    if proposal.sku not in kit_skus:
        return "El SKU no corresponde a un kit clínico del catálogo vigente."
    mentioned = {
        sku.casefold()
        for sku in kit_skus
        if re.search(rf"(?<![\w-]){re.escape(sku.casefold())}(?![\w-])", text)
    }
    mentioned.update(re.findall(r"\bkit-[\w-]+", text))
    if mentioned and mentioned != {proposal.sku.casefold()}:
        return "El SKU propuesto no coincide con un único kit mencionado."

    quantities = [
        _number(value)
        for value in re.findall(
            rf"(?<![\w.,-])({NUMBER})\s+(?:de\s+)?"
            rf"(?:kits?\b|unidades?\b|del\b|{re.escape(proposal.sku.casefold())}\b)",
            text,
        )
    ]
    # A definite singular kit is as precise as "un kit" in natural requests.
    if re.search(r"\bel\s+kit\b", text):
        quantities.append(1)
    uncertain_quantity = re.search(
        rf"\b(?:unos|unas|aproximadamente|alrededor de|cerca de|mas de|menos de|"
        rf"al menos)\s+{NUMBER}\b|"
        rf"(?<![\w:.-]){NUMBER}\s*(?:o|a|-)\s*{NUMBER}(?![\w:.-])|"
        rf"\bentre\s+{NUMBER}\s+y\s+{NUMBER}\b",
        text,
    )
    if (
        proposal.quantity is None
        or proposal.quantity <= 0
        or uncertain_quantity
        or len(quantities) != 1
        or quantities[0] != proposal.quantity
    ):
        return "La cantidad no es única, positiva y explícita en el mensaje."
    if proposal.delivery_date is None:
        return "Falta una fecha y hora de entrega precisa."
    if proposal.delivery_date <= message.received_at or proposal.delivery_date <= now:
        return "El plazo de entrega ya venció."
    if not _deadline_matches(text, message.received_at, proposal.delivery_date):
        return "La fecha propuesta no coincide con un plazo explícito y preciso."
    return None


def _deadline_matches(text: str, received_at: datetime, deadline: datetime) -> bool:
    # Deliberately bounded grammar: unsupported or vague dates are discarded.
    absolute = re.findall(
        r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?"
        r"(?:z|[+-]\d{2}:\d{2})?",
        text,
    )
    if absolute:
        if len(absolute) != 1:
            return False
        try:
            expected = datetime.fromisoformat(absolute[0].upper())
        except ValueError:
            return False
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=CHILE)
            if expected.utcoffset() != expected.replace(fold=1).utcoffset():
                return False
        return deadline == expected

    relative = re.findall(rf"\ben\s+({NUMBER})\s+(?:de\s+)?(horas?|minutos?)\b", text)
    if relative:
        if len(relative) != 1:
            return False
        amount, unit = relative[0]
        seconds = _number(amount) * (3600 if unit.startswith("hora") else 60)
        try:
            return deadline == received_at.astimezone(UTC) + timedelta(seconds=seconds)
        except OverflowError:
            return False

    clock_text = (
        text.replace("de la manana", "am")
        .replace("de la tarde", "pm")
        .replace("de la noche", "pm")
    )
    days = set(re.findall(r"\b(hoy|manana|ayer)\b", clock_text))
    clocks = re.findall(
        rf"\b(?:a las?|antes de las?|hasta las?|para las?)\s+({NUMBER})"
        r"(?::([0-5]\d))?\s*(am|pm)?\b",
        clock_text,
    ) or re.findall(r"\b(\d{1,2}):([0-5]\d)\s*(am|pm)?\b", clock_text)
    if len(days) > 1 or len(clocks) != 1:
        return False
    hour_text, minute_text, meridian = clocks[0]
    hour, minute = _number(hour_text), int(minute_text or 0)
    if meridian:
        if not 1 <= hour <= 12:
            return False
        hour = hour % 12 + (12 if meridian == "pm" else 0)
    elif hour > 23 or (1 <= hour <= 12 and not minute_text):
        return False
    received_local = received_at.astimezone(CHILE)
    day = received_local.date()
    if days:
        day += timedelta(days={"hoy": 0, "manana": 1, "ayer": -1}[days.pop()])
    elif (hour, minute, 0) <= (
        received_local.hour,
        received_local.minute,
        received_local.second,
    ):
        # An undated clock refers to its next occurrence; explicit days never roll.
        day += timedelta(days=1)
    local = deadline.astimezone(CHILE)
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        return False
    return (local.date(), local.hour, local.minute, local.second) == (
        day,
        hour,
        minute,
        0,
    )
