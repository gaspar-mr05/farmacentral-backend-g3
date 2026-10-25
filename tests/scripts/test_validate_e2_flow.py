import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    Lot,
    OrderSource,
    OrderStatus,
    Unit,
)
from app.schemas.purchase_orders import PurchaseOrderState
from app.services.production.kits import KitProductionService
from app.services.production.lineage import has_official_lineage
from app.services.recalls import RecallResponseService
from scripts import validate_e2_flow as module
from tests.scripts.test_verify_e1_traceability import (
    recorded_delivery as recorded_delivery,
)
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory
from tests.support.traceability import create_traceability_scenario


@pytest.mark.anyio
@pytest.mark.parametrize("with_stock", [False, True])
async def test_recall_without_deliveries_or_derivatives_is_valid(
    recall_database, with_stock
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        root = inventory.lot("raw", "RAW")
        unit = inventory.unit("stock", root) if with_stock else None
        recall = inventory.recall(root)
        session.commit()
        client = AsyncMock()
        client.get_spaces.return_value = [
            {"_id": inventory.locations["quarantine"].code, "quarantine": True}
        ]
        client.get_space_products.return_value = (
            [{"_id": unit.external_unit_id}] if unit else []
        )
        await RecallResponseService(client, session).respond(recall.recall_id)
        result = await module.check_recall(session, recall.recall_id, client)
        assert result["status"] == "passed"
        assert result["stock"] == (1 if with_stock else 0)
        assert result["orders"] == result["derived_batches"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("retry,expired", [(True, False), (False, False), (True, True)])
async def test_recall_failure_is_pending_only_when_retry_can_still_run(
    db_session, retry, expired
):
    now = datetime.now(UTC)
    event = InboundEvent(
        source=InboundEventSource.RECALL,
        external_event_id="rcl-retry",
        status=InboundEventStatus.FAILED,
        last_error="Origin batch not synchronized",
        next_retry_at=now + timedelta(seconds=30) if retry else None,
        payload={
            "recallId": "rcl-retry",
            "batch": "L-RAW",
            "sku": "RAW",
            "reason": "Calidad",
            "issuedAt": (now - timedelta(minutes=40)).isoformat(),
            "deadline": (now + timedelta(minutes=-1 if expired else 20)).isoformat(),
        },
    )
    db_session.add(event)
    db_session.flush()
    result = await module.check_recall(db_session, event.external_event_id, AsyncMock())
    assert result["status"] == ("pending" if retry and not expired else "failed")


@pytest.mark.parametrize("problem", ["batch", "consumption", "cycle"])
def test_official_lineage_detects_missing_history(db_session, problem):
    scenario = create_traceability_scenario(db_session)
    kit = db_session.get(Lot, scenario.kit_lot_id)
    raw = db_session.get(Lot, scenario.raw_lot_id)
    assert has_official_lineage(kit)

    if problem == "batch":
        raw.external_lot_id = "unreported:old-unit"
    elif problem == "consumption":
        kit.produced_by.inputs[0].quantity_consumed = 2
    else:
        # Detect a cycle even if all batch strings are official-looking.
        item = kit.produced_by.inputs[0]
        item.input_lot = kit
    assert not has_official_lineage(kit)


def test_validation_production_excludes_legacy_inputs_only_when_requested(db_session):
    scenario = create_traceability_scenario(db_session)
    raw = db_session.get(Lot, scenario.raw_lot_id)
    unit = db_session.scalar(select(Unit).where(Unit.lot_id == scenario.kit_lot_id))
    raw.external_lot_id = "unreported:legacy-input"
    db_session.flush()

    normal = KitProductionService(AsyncMock(), db_session, raw_material_source="supply")
    strict = KitProductionService(
        AsyncMock(),
        db_session,
        raw_material_source="supply",
        require_official_batches=True,
    )
    assert normal._available_units(unit.lot.product.sku) == [unit]
    assert strict._available_units(unit.lot.product.sku) == []
    raw.external_lot_id = scenario.raw_lot_external_id
    assert strict._available_units(unit.lot.product.sku) == [unit]


@pytest.mark.anyio
@pytest.mark.parametrize("problem", [None, "supplier", "quantity", "deadline", "batch"])
async def test_order_validation_compares_real_state_and_lineage(
    db_session, recorded_delivery, problem
):
    unit, order, scenario = recorded_delivery
    order.source = OrderSource.SFTP
    order.external_order_id = uuid4().hex[:24]
    order.external_customer_id = "4"
    order.delivery_deadline = datetime.now(UTC) + timedelta(hours=1)
    official = SimpleNamespace(
        supplier_id="3",
        customer_id="4",
        sku=unit.lot.product.sku,
        quantity=1,
        dispatched_quantity=1,
        state=PurchaseOrderState.FULFILLED,
        delivery_deadline=order.delivery_deadline,
    )
    if problem == "supplier":
        official.supplier_id = "5"
    elif problem == "quantity":
        official.dispatched_quantity = 0
    elif problem == "deadline":
        official.delivery_deadline = datetime.now(UTC) - timedelta(days=1)
    elif problem == "batch":
        db_session.get(Lot, scenario.raw_lot_id).external_lot_id = "unreported:legacy"
    db_session.flush()
    client = AsyncMock()
    client.get_order.return_value = official

    result = await module.check_order(db_session, order.external_order_id, client, 3)

    expected = (
        "passed"
        if problem is None
        else "pending"
        if problem == "quantity"
        else "failed"
    )
    assert result["status"] == expected
    if problem is None:
        assert result["deliveries"][0]["unit_id"] == unit.external_unit_id
        assert not db_session.dirty
    client.update_order_state.assert_not_called()


@pytest.mark.anyio
async def test_pending_order_is_observed_without_processing(
    db_session, recorded_delivery
):
    _, order, _ = recorded_delivery
    order.source = OrderSource.PUBSUB
    order.external_order_id = uuid4().hex[:24]
    order.status = OrderStatus.RECEIVED
    db_session.flush()
    client = AsyncMock()

    result = await module.check_order(db_session, order.external_order_id, client, 3)

    assert result["status"] == "pending"
    client.get_order.assert_not_called()
    assert order.status == OrderStatus.RECEIVED


@pytest.mark.anyio
@pytest.mark.parametrize(
    "problem", [None, "report", "notification", "physical_stock", "late"]
)
async def test_recall_validation_reads_a_real_persisted_response(
    recall_database, problem
):
    with Session(recall_database) as session:
        scenario = create_traceability_scenario(session)
        inventory = RecallInventory(session)
        root = session.get(Lot, scenario.raw_lot_id)
        kit = session.get(Lot, scenario.kit_lot_id)
        delivered = session.scalar(select(Unit).where(Unit.lot_id == kit.id))
        stock = inventory.unit("stock", kit)
        inventory.order((delivered, True))
        recall = inventory.recall(root)
        session.commit()
        client = AsyncMock()
        client.get_spaces.return_value = [
            {"_id": inventory.locations["quarantine"].code, "quarantine": True}
        ]
        client.get_space_products.return_value = [{"_id": stock.external_unit_id}]
        await RecallResponseService(client, session).respond(recall.recall_id)
        if problem == "report":
            recall.last_report = {**recall.last_report, "inStock": []}
        elif problem == "notification":
            notification = recall.orders[0].notification
            notification.payload = {**notification.payload, "orderId": "wrong-order"}
        elif problem == "physical_stock":
            client.get_space_products.return_value = []
        elif problem == "late":
            recall.reported_at = recall.deadline + timedelta(seconds=1)
        session.commit()
        recall_id = recall.recall_id
    with Session(recall_database) as observer:
        observer.execute(text("SET TRANSACTION READ ONLY"))
        result = await module.check_recall(observer, recall_id, client)
        expected = (
            "passed"
            if problem is None
            else "pending"
            if problem == "physical_stock"
            else "failed"
        )
        assert result["status"] == expected
        assert not observer.new and not observer.dirty and not observer.deleted
    # Only the worker setup performed mutations; the validator only did GETs.
    assert client.report_recall.await_count == 1
    assert client.notify_recall.await_count == 1
    assert client.move_product.await_count == 1


@pytest.mark.anyio
async def test_expired_recall_fails_without_replaying_it(db_session):
    inventory = RecallInventory(db_session)
    recall = inventory.recall(inventory.lot("root", "RAW"))
    recall.issued_at = datetime.now(UTC) - timedelta(hours=1)
    recall.deadline = recall.issued_at + timedelta(minutes=30)
    db_session.flush()
    client = AsyncMock()

    result = await module.check_recall(db_session, recall.recall_id, client)

    assert result["status"] == "failed"
    assert "venció" in result["detail"]
    client.report_recall.assert_not_called()


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        (f"https://{module.DEV_HOST}/farma-central/", True),
        ("https://prod.proyecto.2026-2.tallerdeintegracion.cl/farma-central/", False),
        (f"https://{module.DEV_HOST}.example.org/", False),
        (f"http://{module.DEV_HOST}/", False),
    ],
)
def test_only_official_development_is_allowed(url, allowed):
    settings = SimpleNamespace(
        farma_central_base_url=url, effective_purchase_orders_base_url=url
    )
    if allowed:
        module.require_development(settings)
    else:
        with pytest.raises(ValueError, match="desarrollo"):
            module.require_development(settings)


def test_linkage_is_optional_and_does_not_require_both_channels():
    checks = {
        "order:first": {"status": "passed", "channel": "sftp"},
        "order:second": {"status": "passed", "channel": "pubsub"},
        "recall": {
            "status": "passed",
            "report": {"deliveries": [{"orderId": "other"}]},
        },
    }
    assert (
        module.check_flow(checks, ["first", "second"], "recall")["status"] == "skipped"
    )
    checks["recall"]["report"]["deliveries"] = [
        {"orderId": "first"},
        {"orderId": "second"},
    ]
    assert module.check_flow(checks, ["first"], "recall")["status"] == "passed"
    assert (
        module.check_flow(checks, ["first", "second"], "recall")["status"] == "passed"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [None, "recall", "order", "message"])
async def test_default_command_is_read_only_and_does_not_claim_completion(
    recall_database, monkeypatch, capsys, scope
):
    argv = ["validate_e2_flow"]
    if scope:
        argv.extend([f"--{scope}-id", "a" * 24 if scope == "order" else "rcl-scope"])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "SessionLocal", lambda: Session(recall_database))
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            farma_central_base_url=f"https://{module.DEV_HOST}/farma-central/",
            effective_purchase_orders_base_url=f"https://{module.DEV_HOST}/ordenes-compra/",
            farma_central_group=3,
        ),
    )
    monkeypatch.setattr(
        module, "database_check", lambda session: module.result("passed", "test schema")
    )
    prepare = AsyncMock()
    monkeypatch.setattr(module, "prepare", prepare)
    remote = AsyncMock()
    monkeypatch.setattr(module, "FarmaCentralClient", lambda: remote)
    monkeypatch.setattr(module, "PurchaseOrdersClient", lambda: remote)
    monkeypatch.setattr(
        module,
        "check_recall",
        AsyncMock(return_value=module.result("passed", "Recall válido")),
    )
    monkeypatch.setattr(
        module,
        "check_recall_api",
        AsyncMock(return_value=module.result("passed", "API de recalls válida")),
    )
    monkeypatch.setattr(
        module,
        "check_order",
        AsyncMock(return_value=module.result("passed", "OC cumplida", channel="sftp")),
    )
    monkeypatch.setattr(
        module,
        "check_message",
        AsyncMock(return_value=module.result("passed", "Mensaje auditado")),
    )
    http = AsyncMock()
    http.get.return_value = SimpleNamespace(status_code=200)
    http.__aenter__.return_value = http
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: http)
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        provisional = inventory.lot("pending", "RAW")
        provisional.external_lot_id = "unreported:legacy"
        unit = inventory.unit("old", provisional, status="consumed")
        unit_id = unit.id
        session.commit()

    assert await module.main() == (0 if scope else 2)

    evidence = json.loads(capsys.readouterr().out)
    assert evidence["llm_calls"] == 0
    if scope == "order":
        assert evidence["checks"]["order:" + "a" * 24]["status"] == "passed"
        assert evidence["checks"]["recall"]["status"] == "skipped"
    else:
        assert evidence["checks"]["order"]["status"] == (
            "skipped" if scope else "pending"
        )
    if scope == "recall":
        assert evidence["checks"]["recall"]["status"] == "passed"
    if scope == "message":
        assert evidence["checks"]["message:rcl-scope"]["status"] == "passed"
    assert evidence["checks"]["health"]["status"] == "passed"
    assert evidence["inventory"]["unresolved_historical_units"] == 1
    prepare.assert_not_called()
    http.get.assert_awaited_once_with("http://127.0.0.1:8000/api/health")
    with Session(recall_database) as session:
        assert session.get(Unit, unit_id).lot.external_lot_id == "unreported:legacy"
        assert session.get(Unit, unit_id).status == "consumed"


@pytest.mark.anyio
async def test_list_ids_only_reads_database_without_remote_calls(
    recall_database, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["validate_e2_flow", "--list-ids"])
    monkeypatch.setattr(module, "SessionLocal", lambda: Session(recall_database))
    monkeypatch.setattr(module, "require_development", lambda settings: None)
    monkeypatch.setattr(
        module, "database_check", lambda session: module.result("passed", "test schema")
    )
    forbidden = Mock(side_effect=AssertionError("No remote calls during discovery"))
    monkeypatch.setattr(module, "FarmaCentralClient", forbidden)
    monkeypatch.setattr(module, "PurchaseOrdersClient", forbidden)
    monkeypatch.setattr(module.httpx, "AsyncClient", forbidden)
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        unit = inventory.unit("stock", inventory.lot("kit"))
        order = inventory.order((unit, False))
        order_id = order.external_order_id
        session.add(
            InboundEvent(
                source=InboundEventSource.MESSAGING,
                external_event_id="msg-visible",
                payload={"text": "private message text"},
            )
        )
        session.commit()

    assert await module.main() == 0
    output = capsys.readouterr().out
    evidence = json.loads(output)
    assert evidence["available_ids"]["orders"]["sftp"][0]["order_id"] == order_id
    assert (
        evidence["available_ids"]["events"]["messaging"][0]["event_id"] == "msg-visible"
    )
    assert "private message text" not in output
    assert set(evidence["checks"]) == {"database"}
    forbidden.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("problem", [None, "official", "decision", "dispatched"])
async def test_rejected_oc_can_pass_decision_check_but_not_fulfillment(
    db_session, problem
):
    inventory = RecallInventory(db_session)
    unit = inventory.unit("stock", inventory.lot("kit"))
    order = inventory.order((unit, False))
    order.status = OrderStatus.REJECTED
    order.decision_at = inventory.now
    order.decision_reason = "Insufficient capacity"
    order.delivery_deadline = inventory.now + timedelta(hours=2)
    official = SimpleNamespace(
        supplier_id="3",
        customer_id=order.external_customer_id,
        sku=unit.lot.product.sku,
        quantity=1,
        dispatched_quantity=0,
        delivery_deadline=order.delivery_deadline,
        state=PurchaseOrderState.REJECTED,
    )
    if problem == "official":
        official.state = PurchaseOrderState.ACCEPTED
    elif problem == "decision":
        order.decision_at = None
    elif problem == "dispatched":
        order.items[0].assigned_units[0].dispatched_at = inventory.now
    db_session.flush()
    client = AsyncMock()
    client.get_order.return_value = official
    result = await module.check_order(db_session, order.external_order_id, client, 3)
    assert result["status"] == ("passed" if problem is None else "failed")
    if problem is None:
        assert result["fulfilled"] is False
    assert not db_session.dirty
    client.update_order_state.assert_not_awaited()


@pytest.mark.anyio
async def test_transient_error_does_not_prevent_other_checks_and_hides_body():
    from app.clients.farma_central import FarmaCentralHTTPError

    check = AsyncMock(side_effect=FarmaCentralHTTPError(503, "private external body"))
    result = await module.observe(check)
    assert result["status"] == "pending"
    assert result["http_status"] == 503
    assert "private external body" not in json.dumps(result)


@pytest.mark.parametrize(
    "argv",
    [
        ["--message-id", " "],
        ["--recall-id", ""],
        ["--list-ids", "--step19"],
    ],
)
def test_invalid_selection_is_rejected_before_queries(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["validate_e2_flow", *argv])
    with pytest.raises(SystemExit) as exc:
        module.arguments()
    assert exc.value.code == 2


def test_existing_output_is_rejected_before_any_actions(tmp_path, monkeypatch):
    output = tmp_path / ".env"
    output.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["validate_e2_flow", "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        module.arguments()
    assert exc.value.code == 2
    assert output.read_text(encoding="utf-8") == "preserve"
