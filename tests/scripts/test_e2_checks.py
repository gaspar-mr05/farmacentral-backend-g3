from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    MessagingDecision,
    OrderSource,
)
from app.schemas.purchase_orders import PurchaseOrderState
from app.services.recalls import RecallInvestigationService
from app.services.recalls.query import RecallQueryService
from scripts import e2_checks as module
from tests.scripts.test_verify_e1_traceability import (
    recorded_delivery as recorded_delivery,
)
from tests.support.recalls import RecallInventory


def message(session, *, decision=MessagingDecision.DISCARDED):
    now = datetime.now(UTC)
    event = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id=f"msg-{uuid4().hex}",
        status=InboundEventStatus.PROCESSED,
        decision=decision,
        reason="No es un pedido" if decision == MessagingDecision.DISCARDED else None,
        responded_at=now,
        llm_output={"is_order": decision == MessagingDecision.ORDER},
        payload={
            "messageId": "msg-test",
            "sender": {"name": "Farmacia", "phone": "+56911111111"},
            "text": "Texto recibido",
            "receivedAt": now.isoformat(),
        },
    )
    event.payload["messageId"] = event.external_event_id
    session.add(event)
    return event


@pytest.mark.anyio
@pytest.mark.parametrize("problem", [None, "late", "reason", "linked"])
async def test_discarded_message_is_read_only_and_does_not_need_llm(
    db_session, problem
):
    event = message(db_session)
    if problem == "late":
        event.responded_at += timedelta(minutes=31)
    elif problem == "reason":
        event.reason = ""
    elif problem == "linked":
        event.external_order_id = "a" * 24
    db_session.flush()
    client = AsyncMock()

    result = await module.check_message(db_session, event.external_event_id, client, 3)

    assert result["status"] == ("passed" if problem is None else "failed")
    assert not db_session.dirty
    client.get_order.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "problem", [None, "interpretation", "official", "decision", "official_rejection"]
)
async def test_message_checks_linked_oc_without_interpreting_again(
    db_session, recorded_delivery, problem
):
    unit, order, _ = recorded_delivery
    event = message(db_session, decision=MessagingDecision.ORDER)
    order.source = OrderSource.MESSAGING
    order.external_order_id = uuid4().hex[:24]
    order.external_customer_id = "4"
    order.buyer_name = event.payload["sender"]["name"]
    order.delivery_deadline = datetime.now(UTC) + timedelta(hours=1)
    order.decision_at = datetime.now(UTC)
    order.decision_reason = "Accepted with stock"
    event.order = order
    event.external_order_id = order.external_order_id
    event.interpreted_sku = unit.lot.product.sku
    event.interpreted_quantity = 1
    event.interpreted_delivery_date = order.delivery_deadline
    official = SimpleNamespace(
        supplier_id="3",
        customer_id="4",
        sku=event.interpreted_sku,
        quantity=1,
        delivery_deadline=order.delivery_deadline,
        state=PurchaseOrderState.FULFILLED,
    )
    if problem == "interpretation":
        event.interpreted_quantity = 2
    elif problem == "official":
        official.supplier_id = "7"
    elif problem == "decision":
        order.decision_at = None
    elif problem == "official_rejection":
        official.state = PurchaseOrderState.REJECTED
    db_session.flush()
    client = AsyncMock()
    client.get_order.return_value = official

    result = await module.check_message(db_session, event.external_event_id, client, 3)

    assert result["status"] == (
        "passed"
        if problem is None
        else "pending"
        if problem == "decision"
        else "failed"
    )
    assert not db_session.dirty
    client.update_order_state.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("problem", [None, "units", "sku", "state", "http"])
async def test_public_recall_api_compares_db_and_http(db_session, problem):
    inventory = RecallInventory(db_session)
    root = inventory.lot("root", "RAW")
    inventory.unit("stock", root)
    recall = inventory.recall(root)
    RecallInvestigationService(db_session).investigate(recall.recall_id)
    expected = RecallQueryService(db_session).get_recall(recall.recall_id)
    detail = expected.model_dump(mode="json")
    listing = [expected.model_dump(mode="json", exclude={"units"})]
    if problem == "units":
        detail["units"] = []
    elif problem == "sku":
        detail["units"][0]["sku"] = "OTHER"
    elif problem == "state":
        detail["status"] = "failed"
    requests = []

    def handler(request):
        requests.append(request)
        body = listing if request.url.path == "/api/recalls" else detail
        return httpx.Response(503 if problem == "http" else 200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await module.check_recall_api(
            db_session, recall.recall_id, client, "https://backend.test"
        )

    assert result["status"] == (
        "passed"
        if problem is None
        else "pending"
        if problem in {"state", "http"}
        else "failed"
    )
    assert [request.method for request in requests] == ["GET", "GET"]
    assert not db_session.dirty


@pytest.mark.anyio
@pytest.mark.parametrize(
    "problem", [None, "stopped", "restart", "replenishment", "systemd", "timeout"]
)
async def test_workers_require_five_original_services_and_never_start_them(
    monkeypatch, problem
):
    blocks = []
    for worker in module.WORKERS:
        state = (
            "inactive"
            if problem == "stopped" and worker == "process_messaging"
            else "active"
        )
        restart = "no" if problem == "restart" else "on-failure"
        blocks.append(
            f"Id=farmacentral-worker@{worker}.service\nLoadState=loaded\nActiveState={state}\nSubState=running\nRestart={restart}"
        )
    process = SimpleNamespace(
        returncode=1 if problem == "systemd" else 0,
        communicate=AsyncMock(return_value=("\n\n".join(blocks).encode(), b"")),
        kill=Mock(),
    )
    if problem == "timeout":
        process.communicate.side_effect = [TimeoutError, (b"", b"")]
    command = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", command)

    result = await module.check_workers(
        include_replenishment=problem == "replenishment"
    )

    assert result["status"] == (
        "passed" if problem is None else "pending" if problem == "systemd" else "failed"
    )
    assert command.call_args.args[:2] == ("systemctl", "show")
    assert len([arg for arg in command.call_args.args if arg.endswith(".service")]) == (
        6 if problem == "replenishment" else 5
    )
    assert process.kill.call_count == (1 if problem == "timeout" else 0)


def test_step19_does_not_confuse_rejection_with_fulfillment_or_require_linked_recall():
    passed = module.result("passed", "checked")
    checks = {
        name: passed
        for name in (
            "database",
            "health",
            "recall",
            "recall_api",
            "workers",
            "message:msg",
        )
    }
    checks["order:sftp"] = {**passed, "channel": "sftp", "fulfilled": False}
    checks["order:pubsub"] = {**passed, "channel": "pubsub", "fulfilled": True}
    checks["flow"] = module.result("skipped", "independent recalls")
    assert module.check_step19(checks)["missing"] == ["sftp"]
    checks["order:sftp"]["fulfilled"] = True
    assert module.check_step19(checks)["status"] == "passed"
