from datetime import UTC, datetime

import pytest

from app.clients.purchase_orders import PurchaseOrdersHTTPError
from app.db.messaging_messages import add_inbound_message
from app.models import InboundEvent, InboundEventSource, MessagingDecision
from app.schemas.messaging import MessagingWebhookPayload
from app.services.messaging import InquiryResolutionService
from tests.support.farma_central import make_settings
from tests.support.orders import make_purchase_order

FIRST_ORDER_ID = "64abc123def456789012abcd"
SECOND_ORDER_ID = "74abc123def456789012abcd"
MORNING = datetime(2026, 11, 12, 13, tzinfo=UTC)
AFTERNOON = datetime(2026, 11, 12, 18, tzinfo=UTC)
QUERY_TIME = datetime(2026, 11, 12, 20, tzinfo=UTC)


class FakeOrders:
    def __init__(self, *, supplier_id="3", error=None):
        self.requested = []
        self.supplier_id = supplier_id
        self.error = error

    async def get_order(self, order_id):
        self.requested.append(order_id)
        if self.error:
            raise self.error
        return make_purchase_order(
            id=order_id,
            proveedor=self.supplier_id,
            sku="KIT-CURACION",
            cantidad=2,
            despachado=1,
            estado="aceptada",
            vencimiento="2026-11-13T02:00:00Z",
        )


def add_message(
    db_session,
    *,
    message_id,
    phone,
    text,
    occurred_at,
    decision,
    order_id=None,
    sku=None,
    quantity=None,
):
    payload = MessagingWebhookPayload.model_validate(
        {
            "messageId": message_id,
            "sender": {"name": "Farmacia", "phone": phone},
            "text": text,
            "receivedAt": occurred_at.isoformat(),
        }
    )
    event = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id=message_id,
        payload=payload.model_dump(mode="json", by_alias=True),
        decision=decision,
        external_order_id=order_id,
        interpreted_sku=sku,
        interpreted_quantity=quantity,
    )
    db_session.add(event)
    add_inbound_message(db_session, event, payload, make_settings())
    db_session.flush()
    return event


@pytest.mark.anyio
async def test_natural_reference_selects_the_correct_prior_order_and_snapshots_it(
    db_session,
):
    add_message(
        db_session,
        message_id="morning-order",
        phone="+56955550123",
        text="Quiero dos kits de curación",
        occurred_at=MORNING,
        decision=MessagingDecision.ORDER,
        order_id=FIRST_ORDER_ID,
        sku="KIT-CURACION",
        quantity=2,
    )
    add_message(
        db_session,
        message_id="afternoon-order",
        phone="+56955550123",
        text="Quiero tres kits respiratorios",
        occurred_at=AFTERNOON,
        decision=MessagingDecision.ORDER,
        order_id=SECOND_ORDER_ID,
        sku="KIT-RESP-ADULTO",
        quantity=3,
    )
    inquiry = add_message(
        db_session,
        message_id="inquiry",
        phone="+56955550123",
        text="¿Ya salió el pedido de dos kits de curación de la mañana?",
        occurred_at=QUERY_TIME,
        decision=MessagingDecision.INQUIRY,
    )
    orders = FakeOrders()
    service = InquiryResolutionService(orders, db_session, group_id=3)

    response = await service.resolve(inquiry, now=QUERY_TIME)

    assert "1 de 2" in response
    assert orders.requested == [FIRST_ORDER_ID]
    assert inquiry.consulted_external_order_id == FIRST_ORDER_ID
    assert inquiry.resolution_evidence["method"] == "description"
    assert inquiry.resolution_evidence["criteria"] == ["sku", "quantity", "time"]
    assert inquiry.resolution_evidence["responseText"] == response
    assert inquiry.order_status_snapshot["observedAt"] == QUERY_TIME.isoformat()
    assert inquiry.order_status_snapshot["estado"] == "aceptada"
    assert inquiry.order_status_snapshot["despachado"] == 1


@pytest.mark.anyio
async def test_cited_order_is_looked_up_without_prior_phone_history(db_session):
    add_message(
        db_session,
        message_id="own-order",
        phone="+56955550123",
        text="Quiero dos kits",
        occurred_at=MORNING,
        decision=MessagingDecision.ORDER,
        order_id=FIRST_ORDER_ID,
        sku="KIT-CURACION",
        quantity=2,
    )
    add_message(
        db_session,
        message_id="other-order",
        phone="+56944440123",
        text="Quiero dos kits",
        occurred_at=MORNING,
        decision=MessagingDecision.ORDER,
        order_id=SECOND_ORDER_ID,
        sku="KIT-CURACION",
        quantity=2,
    )
    inquiry = add_message(
        db_session,
        message_id="unsafe-inquiry",
        phone="+56955550123",
        text=f"¿Cómo va la OC {SECOND_ORDER_ID}?",
        occurred_at=QUERY_TIME,
        decision=MessagingDecision.INQUIRY,
    )
    orders = FakeOrders()
    service = InquiryResolutionService(orders, db_session, group_id=3)

    response = await service.resolve(inquiry)

    assert "1 de 2" in response
    assert orders.requested == [SECOND_ORDER_ID]
    assert inquiry.consulted_external_order_id == SECOND_ORDER_ID
    assert inquiry.resolution_evidence["status"] == "resolved"
    assert inquiry.resolution_evidence["method"] == "cited_order_id"
    assert inquiry.resolution_evidence["criteria"] == ["order_id"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("orders", "expected_status"),
    [
        (FakeOrders(supplier_id="7"), "supplier_mismatch"),
        (FakeOrders(error=PurchaseOrdersHTTPError(404)), "order_not_found"),
    ],
)
async def test_cited_order_must_exist_and_belong_to_the_group(
    db_session, orders, expected_status
):
    inquiry = add_message(
        db_session,
        message_id=f"inquiry-{expected_status}",
        phone="+56955550123",
        text=f"¿Cómo va la OC {SECOND_ORDER_ID}?",
        occurred_at=QUERY_TIME,
        decision=MessagingDecision.INQUIRY,
    )
    service = InquiryResolutionService(orders, db_session, group_id=3)

    response = await service.resolve(inquiry)

    assert "No pude identificar" in response
    assert orders.requested == [SECOND_ORDER_ID]
    assert inquiry.consulted_external_order_id is None
    assert inquiry.order_status_snapshot is None
    assert inquiry.resolution_evidence["status"] == expected_status


@pytest.mark.anyio
async def test_ambiguous_reference_does_not_guess_the_latest_order(db_session):
    for message_id, order_id, occurred_at in (
        ("first", FIRST_ORDER_ID, MORNING),
        ("second", SECOND_ORDER_ID, AFTERNOON),
    ):
        add_message(
            db_session,
            message_id=message_id,
            phone="+56955550123",
            text="Quiero dos kits",
            occurred_at=occurred_at,
            decision=MessagingDecision.ORDER,
            order_id=order_id,
            sku="KIT-CURACION",
            quantity=2,
        )
    inquiry = add_message(
        db_session,
        message_id="ambiguous",
        phone="+56955550123",
        text="¿Cómo va mi pedido?",
        occurred_at=QUERY_TIME,
        decision=MessagingDecision.INQUIRY,
    )
    orders = FakeOrders()
    service = InquiryResolutionService(orders, db_session, group_id=3)

    response = await service.resolve(inquiry)

    assert "No pude identificar" in response
    assert orders.requested == []
    assert inquiry.resolution_evidence["status"] == "ambiguous"
    assert inquiry.resolution_evidence["candidateOrderIds"] == [
        FIRST_ORDER_ID,
        SECOND_ORDER_ID,
    ]
