from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.clients.llm_gateway import LLMGatewayClient, LLMGatewayResult
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    Order,
    Product,
)
from app.models.product import ProductCategory
from app.schemas.llm_gateway import LLMCatalogItem, LLMOrderProposal
from app.schemas.messaging import MessagingWebhookPayload
from app.services.message_interpretation import MessageInterpretationService


@pytest.mark.anyio
async def test_uses_current_clinical_catalog_without_creating_orders_or_decisions(
    db_session,
):
    for sku, name, category in (
        ("KIT-RESP-ADULTO", "Kit respiratorio adulto", ProductCategory.KIT),
        ("KIT-DIABETES", "Kit de diabetes", ProductCategory.KIT),
        ("INSUMO-1", "Insumo", ProductCategory.INSUMO),
        ("ACOND-1", "Acondicionado", ProductCategory.ACONDICIONADO),
    ):
        db_session.add(Product(sku=sku, name=name, category=category, batch_size=1))
    incoming = MessagingWebhookPayload.model_validate(
        {
            "messageId": "msg_proposal",
            "sender": {"name": "Farmacia", "phone": "+56 9 5555 0123"},
            "text": "Necesito dos kits respiratorios de adulto.",
            "receivedAt": "2026-10-27T16:05:00Z",
        }
    )
    event = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id=incoming.message_id,
        received_at=incoming.received_at,
        payload=incoming.model_dump(mode="json", by_alias=True),
    )
    db_session.add(event)
    db_session.flush()
    original_payload = dict(event.payload)
    proposal = LLMOrderProposal(
        is_order=True,
        sku="KIT-RESP-ADULTO",
        quantity=2,
        delivery_date=None,
        confidence=0.6,
        is_ambiguous=True,
        reason="Falta el plazo.",
    )
    expected = LLMGatewayResult(proposal=proposal, raw_output={"choices": []})
    client = AsyncMock(spec=LLMGatewayClient)
    client.propose_order.return_value = expected

    result = await MessageInterpretationService(client, db_session).propose(incoming)

    client.propose_order.assert_awaited_once_with(
        text=incoming.text,
        received_at=incoming.received_at,
        catalog=[
            LLMCatalogItem(sku="KIT-DIABETES", name="Kit de diabetes"),
            LLMCatalogItem(sku="KIT-RESP-ADULTO", name="Kit respiratorio adulto"),
        ],
    )
    assert result is expected
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0
    db_session.refresh(event)
    assert event.payload == original_payload
    assert event.status is InboundEventStatus.PENDING
    assert event.llm_output is None
    assert event.decision is None
    assert event.order_id is None
