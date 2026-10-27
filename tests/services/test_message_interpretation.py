from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.clients.farma_central import FarmaCentralClient
from app.clients.llm_gateway import (
    LLMGatewayBudgetExceededError,
    LLMGatewayClient,
    LLMGatewayConnectionError,
    LLMGatewayError,
    LLMGatewayHTTPError,
    LLMGatewayInvalidResponseError,
    LLMGatewayResult,
    LLMGatewayTimeoutError,
)
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
    Order,
    Product,
    ProductCategory,
)
from app.schemas.llm_gateway import LLMOrderProposal
from app.services.messaging import MessageInterpretationService
from tests.support.farma_central import catalog_product

RECEIVED = datetime(2026, 10, 27, 16, 5, tzinfo=UTC)
NOW = RECEIVED + timedelta(minutes=1)
DEADLINE = datetime(2026, 10, 27, 22, tzinfo=UTC)
TEXT = (
    "Hola, necesito dos kits respiratorios para adulto, los que traen amoxicilina, "
    "para hoy antes de las 7 de la tarde. Gracias!"
)


@pytest.fixture
def catalog_client():
    client = AsyncMock(spec=FarmaCentralClient)
    client.get_available_products.return_value = [
        catalog_product(sku, sku, sellable=True)
        for sku in (
            "KIT-RESP-ADULTO",
            "KIT-DIABETES",
            "KIT-ANALGESIA",
            "KIT-PEDIA-FEBRIL",
            "KIT-CURACION",
        )
    ]
    return client


@pytest.fixture
def service(db_session, catalog_client):
    for sku, category in (
        ("KIT-RESP-ADULTO", ProductCategory.KIT),
        ("KIT-DIABETES", ProductCategory.KIT),
        ("KIT-ANALGESIA", ProductCategory.KIT),
        ("KIT-PEDIA-FEBRIL", ProductCategory.KIT),
        ("KIT-CURACION", ProductCategory.KIT),
        ("AMOXICILINA", ProductCategory.INSUMO),
        ("AMOX-ACOND", ProductCategory.ACONDICIONADO),
    ):
        db_session.add(Product(sku=sku, name=sku, category=category, batch_size=1))
    db_session.flush()
    client = AsyncMock(spec=LLMGatewayClient)
    return MessageInterpretationService(
        client, db_session, catalog_client=catalog_client
    ), client


def proposal(**overrides):
    return LLMOrderProposal(
        **{
            "is_order": True,
            "sku": "KIT-RESP-ADULTO",
            "quantity": 2,
            "delivery_date": DEADLINE,
            "confidence": 0.95,
            "is_ambiguous": False,
            "reason": "",
            **overrides,
        }
    )


def event(db_session, text=TEXT, received_at=RECEIVED):
    incoming = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id="msg-interpretation",
        payload={
            "messageId": "msg-interpretation",
            "sender": {"name": "Farmacia", "phone": "+56 9 5555 0123"},
            "text": text,
            "receivedAt": received_at.isoformat(),
        },
    )
    db_session.add(incoming)
    db_session.flush()
    return incoming


def gateway_returns(client, value):
    raw = {"choices": [{"message": {"content": value.model_dump_json()}}]}
    client.propose_order.return_value = LLMGatewayResult(value, raw)
    return raw


@pytest.mark.anyio
@pytest.mark.parametrize("confidence", [0.8, 0.95, 1.0])
async def test_official_example_is_audited_without_accepting_or_creating_an_oc(
    db_session,
    service,
    confidence,
):
    interpreter, client = service
    incoming = event(db_session)
    original = dict(incoming.payload)
    raw = gateway_returns(client, proposal(confidence=confidence))

    result = await interpreter.interpret(incoming, now=NOW)
    db_session.expire_all()

    saved = db_session.get(InboundEvent, incoming.id)
    assert result is saved
    assert saved.decision is MessagingDecision.ORDER
    assert saved.interpreted_sku == "KIT-RESP-ADULTO"
    assert saved.interpreted_quantity == 2
    assert saved.interpreted_delivery_date == DEADLINE
    assert saved.reason is None
    assert saved.llm_output == raw
    assert saved.payload == original
    assert saved.status is InboundEventStatus.PENDING
    assert saved.responded_at is None
    assert saved.processed_at is None
    assert saved.order_id is None
    assert saved.external_order_id is None
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0
    client.propose_order.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("quantity_text", "quantity"),
    [
        ("un", 1),
        ("una", 1),
        ("dos", 2),
        ("un par de", 2),
        ("media docena de", 6),
        ("una docena de", 12),
        ("dieciséis", 16),
        ("20", 20),
        ("100", 100),
    ],
)
async def test_explicit_quantities_in_words_and_digits(
    db_session,
    service,
    quantity_text,
    quantity,
):
    interpreter, client = service
    incoming = event(db_session, f"Quiero {quantity_text} kits para hoy a las 19:00.")
    gateway_returns(client, proposal(quantity=quantity))
    await interpreter.interpret(incoming, now=NOW)
    assert incoming.decision is MessagingDecision.ORDER
    assert incoming.interpreted_quantity == quantity


@pytest.mark.anyio
@pytest.mark.parametrize("quantity_text", ["2 unidades de", "2"])
async def test_quantity_with_an_explicit_sku(db_session, service, quantity_text):
    interpreter, client = service
    incoming = event(
        db_session, f"Quiero {quantity_text} KIT-RESP-ADULTO para hoy a las 19:00."
    )
    gateway_returns(client, proposal())
    await interpreter.interpret(incoming, now=NOW)
    assert incoming.decision is MessagingDecision.ORDER


@pytest.mark.anyio
async def test_portal_order_with_required_singular_kit_and_same_day_clock(
    db_session,
    service,
):
    interpreter, client = service
    received = datetime(2026, 10, 25, 19, 45, 28, tzinfo=UTC)
    deadline = datetime(2026, 10, 25, 21, 15, tzinfo=UTC)
    incoming = event(
        db_session,
        "Buen día, requerimos el kit de analgesia hospitalaria para nuestro box, "
        "antes de las 18:15 hrs.",
        received,
    )
    gateway_returns(
        client,
        proposal(
            sku="KIT-ANALGESIA",
            quantity=1,
            delivery_date=deadline,
            confidence=0.9,
        ),
    )

    await interpreter.interpret(incoming, now=received + timedelta(minutes=1))

    assert incoming.decision is MessagingDecision.ORDER, incoming.reason
    assert incoming.interpreted_sku == "KIT-ANALGESIA"
    assert incoming.interpreted_quantity == 1
    assert incoming.interpreted_delivery_date == deadline


@pytest.mark.anyio
@pytest.mark.parametrize("negated", [False, True])
@pytest.mark.parametrize(
    "request_text",
    [
        "Nos faltan 2 kits de analgesia hospitalaria en bodega.",
        "Nos hacen falta un par de kits de analgesia hospitalaria en bodega.",
    ],
)
async def test_stock_shortage_request_and_its_negation(
    db_session, service, negated, request_text
):
    interpreter, client = service
    received = datetime(2026, 10, 25, 22, 49, 29, tzinfo=UTC)
    deadline = datetime(2026, 10, 26, 2, 45, tzinfo=UTC)
    text = f"{request_text} ¿Pueden llegar las 23:45 hrs?"
    incoming = event(db_session, f"No {text}" if negated else text, received)
    gateway_returns(
        client,
        proposal(sku="KIT-ANALGESIA", quantity=2, delivery_date=deadline),
    )

    await interpreter.interpret(incoming, now=received + timedelta(seconds=1))

    if negated:
        assert incoming.decision is MessagingDecision.DISCARDED
        assert "pedido nuevo" in incoming.reason
        assert incoming.interpreted_sku is None
    else:
        assert incoming.decision is MessagingDecision.ORDER, incoming.reason
        assert incoming.interpreted_sku == "KIT-ANALGESIA"
        assert incoming.interpreted_quantity == 2
        assert incoming.interpreted_delivery_date == deadline
    client.propose_order.assert_awaited_once()
    assert incoming.responded_at is None
    assert incoming.external_order_id is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "text",
    [
        "Hola, nos hacen falta un par de kits para curaciones en la posta. "
        "¿Los pueden traer las 14:00 hrs?",
        "Me hace falta un par de kits para curaciones hoy a las 14:00.",
        "¿Podrían despachar dos kits para curaciones hoy a las 14:00?",
        "Por favor, dos kits de curación para hoy a las 14:00.",
        "¿Me envían un par de kits para curaciones hoy a las 14:00?",
        "Querría dos kits para curaciones hoy a las 14:00.",
        "Les encargo dos kits para curaciones hoy a las 14:00.",
        "Traigan dos kits para curaciones hoy a las 14:00.",
        "¿Pueden ayudarme a conseguir dos kits para curaciones hoy a las 14:00?",
        "Nos urgen dos kits para curaciones hoy a las 14:00.",
    ],
)
async def test_order_intent_does_not_require_a_fixed_request_verb(
    db_session, service, text
):
    interpreter, client = service
    received = datetime(2026, 10, 27, 13, 16, 3, tzinfo=UTC)
    deadline = datetime(2026, 10, 27, 17, tzinfo=UTC)
    incoming = event(db_session, text, received)
    raw = gateway_returns(
        client,
        proposal(
            sku="KIT-CURACION", quantity=2, delivery_date=deadline, confidence=0.9
        ),
    )

    await interpreter.interpret(incoming, now=received + timedelta(minutes=1))

    assert incoming.decision is MessagingDecision.ORDER, incoming.reason
    assert incoming.interpreted_sku == "KIT-CURACION"
    assert incoming.interpreted_quantity == 2
    assert incoming.interpreted_delivery_date == deadline
    assert incoming.reason is None
    assert incoming.llm_output == raw
    assert incoming.external_order_id is None
    assert incoming.responded_at is None
    client.propose_order.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "phrase",
    [
        "Nos hacen falta",
        "Me hace falta",
        "Requerimos",
        "Solicitamos",
        "Nos envíen",
        "Me mandes",
        "Despachen",
        "Nos traigan",
    ],
)
async def test_negated_request_is_discarded_even_if_model_proposes_an_order(
    db_session, service, phrase
):
    interpreter, client = service
    incoming = event(db_session, f"No {phrase} dos kits para hoy a las 19:00.")
    gateway_returns(client, proposal())

    await interpreter.interpret(incoming, now=NOW)

    assert incoming.decision is MessagingDecision.DISCARDED
    assert "pedido nuevo" in incoming.reason
    assert incoming.interpreted_sku is None
    client.propose_order.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "text",
    [
        "Hola, ¿tienen lista de precios actualizada de los kits disponibles?",
        "¿Cuánto cuestan dos kits para entregar hoy a las 19:00?",
        "¿Hay stock de dos kits para hoy a las 19:00? Solo consulto.",
    ],
)
async def test_non_order_classification_is_preserved(db_session, service, text):
    interpreter, client = service
    incoming = event(db_session, text)
    gateway_returns(client, proposal(is_order=False))

    await interpreter.interpret(incoming, now=NOW)

    assert incoming.decision is MessagingDecision.DISCARDED
    assert "pedido nuevo" in incoming.reason
    assert incoming.interpreted_sku is None
    assert incoming.external_order_id is None
    client.propose_order.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "sku", "quantity", "received", "deadline"),
    [
        (
            "Buenas, ¿me pueden despachar dos kits respiratorios para adulto? "
            "Los necesitamos las 01:45 hrs.",
            "KIT-RESP-ADULTO",
            2,
            datetime(2026, 10, 25, 22, 52, 31, tzinfo=UTC),
            datetime(2026, 10, 26, 4, 45, tzinfo=UTC),
        ),
        (
            "Hola, ¿nos pueden mandar el kit pediátrico febril? "
            "Necesitamos que llegue las 01:30 hrs.",
            "KIT-PEDIA-FEBRIL",
            1,
            datetime(2026, 10, 25, 22, 53, 21, tzinfo=UTC),
            datetime(2026, 10, 26, 4, 30, tzinfo=UTC),
        ),
    ],
)
async def test_undated_clock_crosses_midnight_using_chilean_received_at(
    db_session, service, text, sku, quantity, received, deadline
):
    interpreter, client = service
    incoming = event(db_session, text, received)
    gateway_returns(
        client,
        proposal(
            sku=sku,
            quantity=quantity,
            delivery_date=deadline,
        ),
    )

    await interpreter.interpret(incoming, now=received + timedelta(seconds=1))

    assert incoming.decision is MessagingDecision.ORDER, incoming.reason
    assert incoming.interpreted_sku == sku
    assert incoming.interpreted_quantity == quantity
    assert incoming.interpreted_delivery_date == deadline


@pytest.mark.anyio
@pytest.mark.parametrize("day", ["hoy", "ayer"])
async def test_explicit_day_never_rolls_forward_to_accept_a_future_proposal(
    db_session, service, day
):
    interpreter, client = service
    received = datetime(2026, 10, 25, 22, 52, tzinfo=UTC)
    incoming = event(db_session, f"Necesito dos kits para {day} a las 01:45.", received)
    gateway_returns(
        client, proposal(delivery_date=datetime(2026, 10, 26, 4, 45, tzinfo=UTC))
    )

    await interpreter.interpret(incoming, now=received + timedelta(seconds=1))

    assert incoming.decision is MessagingDecision.DISCARDED
    assert "fecha propuesta" in incoming.reason


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("quantity", "deadline", "reason"),
    [
        (None, datetime(2026, 10, 25, 16, 30, tzinfo=UTC), "cantidad"),
        (1, datetime(2026, 10, 25, 16, 30, tzinfo=UTC), "venció"),
        (1, datetime(2026, 10, 26, 16, 30, tzinfo=UTC), "fecha propuesta"),
    ],
)
async def test_wrong_pediatric_proposal_is_not_repaired_or_registered(
    db_session, service, quantity, deadline, reason
):
    interpreter, client = service
    received = datetime(2026, 10, 25, 22, 53, 21, tzinfo=UTC)
    incoming = event(
        db_session,
        "Hola, ¿nos pueden mandar el kit pediátrico febril? "
        "Necesitamos que llegue las 01:30 hrs.",
        received,
    )
    gateway_returns(
        client,
        proposal(
            sku="KIT-PEDIA-FEBRIL",
            quantity=quantity,
            delivery_date=deadline,
            confidence=1.0,
        ),
    )

    await interpreter.interpret(incoming, now=received + timedelta(seconds=1))

    assert incoming.decision is MessagingDecision.DISCARDED
    assert reason in incoming.reason
    assert incoming.interpreted_quantity is None
    assert incoming.interpreted_delivery_date is None
    assert incoming.external_order_id is None


@pytest.mark.anyio
async def test_singular_kit_does_not_validate_a_proposed_plural_quantity(
    db_session,
    service,
):
    interpreter, client = service
    incoming = event(
        db_session,
        "Requerimos el kit respiratorio para hoy a las 19:00.",
    )
    gateway_returns(client, proposal(quantity=2))

    await interpreter.interpret(incoming, now=NOW)

    assert incoming.decision is MessagingDecision.DISCARDED
    assert "cantidad" in incoming.reason.casefold()


@pytest.mark.anyio
async def test_real_component_description_is_sent_with_official_recipe_and_audited(
    db_session, service, catalog_client
):
    interpreter, client = service
    catalog_client.get_available_products.return_value = [
        catalog_product(
            "KIT-ANALGESIA",
            "Kit de Analgesia Hospitalaria",
            sellable=True,
            components=("AMP-KETOPRO-100", "FRA-SUERO-FISIO", "SOB-JERINGA-10"),
        ),
        catalog_product(
            "AMP-KETOPRO-100",
            "Ketoprofeno inyectable 100 mg",
            components=("API-KETOPRO-100",),
        ),
        catalog_product("API-KETOPRO-100", "Ketoprofeno 100 mg"),
        catalog_product("FRA-SUERO-FISIO", "Suero fisiológico"),
        catalog_product("SOB-JERINGA-10", "Jeringas de 10 ml"),
    ]
    received = datetime(2026, 10, 25, 20, 19, 35, tzinfo=UTC)
    deadline = datetime(2026, 10, 25, 23, 45, tzinfo=UTC)
    incoming = event(
        db_session,
        "Buen día, requerimos el kit con ketoprofeno inyectable, suero fisiológico "
        "y jeringas para manejo del dolor. Plazo: las 20:45 hrs.",
        received,
    )
    gateway_returns(
        client,
        proposal(
            sku="KIT-ANALGESIA",
            quantity=1,
            delivery_date=deadline,
        ),
    )

    await interpreter.interpret(incoming, now=received + timedelta(seconds=1))

    assert incoming.decision is MessagingDecision.ORDER, incoming.reason
    assert incoming.interpreted_sku == "KIT-ANALGESIA"
    assert incoming.interpreted_quantity == 1
    assert incoming.interpreted_delivery_date == deadline
    client.propose_order.assert_awaited_once()
    context = client.propose_order.call_args.kwargs["catalog"]
    assert len(context) == 1
    assert "Ketoprofeno inyectable 100 mg" in context[0].components
    assert "Ketoprofeno 100 mg" in context[0].components
    assert "Suero fisiológico" in context[0].components
    assert "Jeringas de 10 ml" in context[0].components
    assert incoming.external_order_id is None
    assert incoming.responded_at is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("text", "overrides", "reason"),
    [
        (TEXT, {"is_order": False}, "pedido nuevo"),
        ("Hola, ¿me confirman si ya despacharon lo que pedí ayer?", {}, "pedido nuevo"),
        ("Necesito ayuda con dos kits para hoy a las 19:00", {}, "pedido nuevo"),
        ("No quiero dos kits para hoy a las 19:00", {}, "pedido nuevo"),
        ("Quiero cancelar dos kits para hoy a las 19:00", {}, "pedido nuevo"),
        (TEXT, {"is_ambiguous": True, "reason": "Adulto o pediátrico"}, "ambigua"),
        (TEXT, {"confidence": 0.79}, "confianza"),
        (TEXT, {"sku": None}, "catálogo"),
        (TEXT, {"sku": "KIT-NO-EXISTE"}, "catálogo"),
        (TEXT, {"sku": "AMOXICILINA"}, "catálogo"),
        (TEXT, {"sku": "AMOX-ACOND"}, "catálogo"),
        ("Quiero dos KIT-DIABETES para hoy a las 19:00", {}, "único kit"),
        ("Quiero dos kits KIT-INEXISTENTE para hoy a las 19:00", {}, "único kit"),
        (
            "Quiero dos kits KIT-DIABETES o KIT-RESP-ADULTO hoy a las 19:00",
            {},
            "único kit",
        ),
        (TEXT, {"quantity": None}, "cantidad"),
        (TEXT, {"quantity": 3}, "cantidad"),
        ("Quiero kits para hoy a las 19:00", {}, "cantidad"),
        ("Quiero varios kits para hoy a las 19:00", {}, "cantidad"),
        ("Quiero unos dos kits para hoy a las 19:00", {}, "cantidad"),
        ("Quiero 2-3 kits para hoy a las 19:00", {}, "cantidad"),
        ("Quiero al menos dos kits para hoy a las 19:00", {}, "cantidad"),
        ("Quiero menos de dos kits para hoy a las 19:00", {}, "cantidad"),
        ("Quiero dos o tres kits para hoy a las 19:00", {"quantity": 3}, "cantidad"),
        (
            "Quiero entre dos y tres kits para hoy a las 19:00",
            {"quantity": 3},
            "cantidad",
        ),
        ("Quiero -2 kits para hoy a las 19:00", {}, "cantidad"),
        ("Quiero 1.2 kits para hoy a las 19:00", {}, "cantidad"),
        (TEXT, {"delivery_date": None}, "fecha y hora"),
        (TEXT, {"delivery_date": RECEIVED}, "venció"),
        (TEXT, {"delivery_date": NOW}, "venció"),
        (TEXT, {"delivery_date": NOW - timedelta(seconds=1)}, "venció"),
        (TEXT, {"delivery_date": DEADLINE + timedelta(days=1)}, "fecha propuesta"),
        (TEXT, {"delivery_date": DEADLINE - timedelta(hours=1)}, "fecha propuesta"),
        ("Quiero dos kits", {}, "fecha propuesta"),
        ("Quiero dos kits para mañana a primera hora", {}, "fecha propuesta"),
        ("Quiero dos kits para hoy a las 7", {}, "fecha propuesta"),
        ("Quiero dos kits para hoy a las 25:00", {}, "fecha propuesta"),
        ("Quiero dos kits para hoy a las 19:00 o a las 20:00", {}, "fecha propuesta"),
        ("Quiero dos kits hoy o mañana a las 19:00", {}, "fecha propuesta"),
        ("Quiero dos kits en tres horas", {}, "fecha propuesta"),
        ("Quiero dos kits en 99999999999999999999 horas", {}, "fecha propuesta"),
        ("Quiero dos kits en tres horas o en cuatro horas", {}, "fecha propuesta"),
    ],
)
async def test_unsafe_interpretations_are_discarded_with_an_auditable_reason(
    db_session,
    service,
    text,
    overrides,
    reason,
):
    interpreter, client = service
    incoming = event(db_session, text)
    original = dict(incoming.payload)
    raw = gateway_returns(client, proposal(**overrides))
    await interpreter.interpret(incoming, now=NOW)
    db_session.refresh(incoming)
    assert incoming.decision is MessagingDecision.DISCARDED
    assert reason.casefold() in incoming.reason.casefold()
    assert incoming.llm_output == raw
    assert incoming.interpreted_sku is None
    assert incoming.interpreted_quantity is None
    assert incoming.interpreted_delivery_date is None
    assert incoming.payload == original
    assert incoming.responded_at is None
    assert incoming.order_id is None
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("phrase", "received", "deadline"),
    [
        ("en tres horas", RECEIVED, RECEIVED + timedelta(hours=3)),
        ("en 30 minutos", RECEIVED, RECEIVED + timedelta(minutes=30)),
        ("en un par de horas", RECEIVED, RECEIVED + timedelta(hours=2)),
        ("mañana a las 09:00", RECEIVED, datetime(2026, 10, 28, 12, tzinfo=UTC)),
        (
            "mañana a las nueve de la mañana",
            RECEIVED,
            datetime(2026, 10, 28, 12, tzinfo=UTC),
        ),
        ("hoy antes de las siete de la tarde", RECEIVED, DEADLINE),
        ("hoy a las 7 pm", RECEIVED, DEADLINE),
        ("2026-10-27T22:00:00Z", RECEIVED, DEADLINE),
        ("2026-10-27T19:00:00-03:00", RECEIVED, DEADLINE),
        ("2026-10-27 19:00", RECEIVED, DEADLINE),
        (
            "hoy a las 19:00",
            datetime(2026, 6, 10, 16, tzinfo=UTC),
            datetime(2026, 6, 10, 23, tzinfo=UTC),
        ),
        (
            "hoy a las 23:00",
            datetime(2026, 10, 28, 1, 5, tzinfo=UTC),
            datetime(2026, 10, 28, 2, tzinfo=UTC),
        ),
        (
            "en tres horas",
            datetime(2026, 9, 6, 3, 30, tzinfo=UTC),
            datetime(2026, 9, 6, 6, 30, tzinfo=UTC),
        ),
    ],
)
async def test_deadlines_use_received_at_and_chilean_timezone(
    db_session,
    service,
    phrase,
    received,
    deadline,
):
    interpreter, client = service
    incoming = event(db_session, f"Necesito dos kits para {phrase}.", received)
    gateway_returns(client, proposal(delivery_date=deadline))
    await interpreter.interpret(incoming, now=received + timedelta(minutes=1))
    assert incoming.decision is MessagingDecision.ORDER, incoming.reason
    assert incoming.interpreted_delivery_date == deadline


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("phrase", "received", "deadline"),
    [
        (
            "2026-09-06 00:30",
            datetime(2026, 9, 5, 16, tzinfo=UTC),
            datetime(2026, 9, 6, 4, 30, tzinfo=UTC),
        ),
        (
            "mañana a las 23:30",
            datetime(2026, 4, 3, 16, tzinfo=UTC),
            datetime(2026, 4, 5, 2, 30, tzinfo=UTC),
        ),
    ],
)
async def test_nonexistent_or_ambiguous_local_times_are_discarded(
    db_session,
    service,
    phrase,
    received,
    deadline,
):
    interpreter, client = service
    incoming = event(db_session, f"Necesito dos kits para {phrase}.", received)
    gateway_returns(client, proposal(delivery_date=deadline))
    await interpreter.interpret(incoming, now=received + timedelta(minutes=1))
    assert incoming.decision is MessagingDecision.DISCARDED
    assert "fecha propuesta" in incoming.reason


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (LLMGatewayTimeoutError("sensitive detail"), "no respondió"),
        (LLMGatewayConnectionError("sensitive detail"), "gateway"),
        (LLMGatewayHTTPError(429), "gateway"),
        (LLMGatewayBudgetExceededError(429), "presupuesto"),
        (LLMGatewayInvalidResponseError("bad JSON", raw_output="{not json"), "formato"),
        (LLMGatewayError("missing configuration"), "gateway"),
    ],
)
async def test_gateway_failure_discards_safely_without_retrying(
    db_session,
    service,
    failure,
    reason,
):
    interpreter, client = service
    incoming = event(db_session)
    client.propose_order.side_effect = failure
    await interpreter.interpret(incoming, now=NOW)
    assert incoming.decision is MessagingDecision.DISCARDED
    assert reason in incoming.reason.casefold()
    assert "sensitive detail" not in incoming.reason
    if isinstance(failure, LLMGatewayInvalidResponseError):
        assert incoming.llm_output == failure.raw_output
    else:
        assert incoming.llm_output["error"] == type(failure).__name__
    client.propose_order.assert_awaited_once()
    assert incoming.interpreted_sku is None
    assert incoming.responded_at is None
    assert incoming.order_id is None


@pytest.mark.anyio
@pytest.mark.parametrize("discarded", [True, False])
async def test_saved_decision_is_reused_without_spending_more_tokens(
    db_session,
    service,
    discarded,
):
    interpreter, client = service
    incoming = event(db_session)
    gateway_returns(client, proposal(is_ambiguous=discarded))
    await interpreter.interpret(incoming, now=NOW)
    first = incoming.decision, incoming.reason, incoming.llm_output
    db_session.expire_all()
    await interpreter.interpret(incoming, now=NOW + timedelta(hours=1))
    assert (incoming.decision, incoming.reason, incoming.llm_output) == first
    client.propose_order.assert_awaited_once()


@pytest.mark.anyio
async def test_malformed_original_message_is_preserved_without_calling_llm(
    db_session,
    service,
):
    interpreter, client = service
    incoming = event(db_session)
    incoming.payload = {"text": "original"}
    await interpreter.interpret(incoming, now=NOW)
    assert incoming.payload == {"text": "original"}
    assert incoming.decision is MessagingDecision.DISCARDED
    assert "mensaje original" in incoming.reason
    client.propose_order.assert_not_awaited()


@pytest.mark.anyio
async def test_wrong_channel_and_responded_messages_are_not_changed(
    db_session, service
):
    interpreter, client = service
    incoming = event(db_session)
    incoming.source = InboundEventSource.PUBSUB
    with pytest.raises(ValueError, match="Only messaging"):
        await interpreter.interpret(incoming, now=NOW)
    incoming.source = InboundEventSource.MESSAGING
    incoming.responded_at = NOW
    with pytest.raises(ValueError, match="responded"):
        await interpreter.interpret(incoming, now=NOW)
    assert incoming.decision is None
    client.propose_order.assert_not_awaited()
