import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
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


def test_flow_cannot_pass_with_unrelated_recall_or_one_channel():
    checks = {
        "order:first": {"status": "passed", "channel": "sftp"},
        "order:second": {"status": "passed", "channel": "pubsub"},
        "recall": {
            "status": "passed",
            "report": {"deliveries": [{"orderId": "other"}]},
        },
    }
    assert (
        module.check_flow(checks, ["first", "second"], "recall")["status"] == "failed"
    )
    checks["recall"]["report"]["deliveries"] = [
        {"orderId": "first"},
        {"orderId": "second"},
    ]
    assert module.check_flow(checks, ["first"], "recall")["status"] == "pending"
    assert (
        module.check_flow(checks, ["first", "second"], "recall")["status"] == "passed"
    )


@pytest.mark.anyio
async def test_default_command_is_read_only_and_does_not_claim_completion(
    recall_database, monkeypatch, capsys
):
    monkeypatch.setattr(sys, "argv", ["validate_e2_flow"])
    monkeypatch.setattr(module, "SessionLocal", lambda: Session(recall_database))
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: SimpleNamespace(
            farma_central_base_url=f"https://{module.DEV_HOST}/farma-central/",
            effective_purchase_orders_base_url=f"https://{module.DEV_HOST}/ordenes-compra/",
        ),
    )
    monkeypatch.setattr(
        module, "database_check", lambda session: module.result("passed", "test schema")
    )
    prepare = AsyncMock()
    monkeypatch.setattr(module, "prepare", prepare)
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

    assert await module.main() == 2

    evidence = json.loads(capsys.readouterr().out)
    assert evidence["llm_calls"] == 0
    assert evidence["checks"]["order"]["status"] == "pending"
    assert evidence["checks"]["health"]["status"] == "passed"
    assert evidence["inventory"]["unresolved_historical_units"] == 1
    prepare.assert_not_called()
    http.get.assert_awaited_once_with("http://127.0.0.1:8000/api/health")
    with Session(recall_database) as session:
        assert session.get(Unit, unit_id).lot.external_lot_id == "unreported:legacy"
        assert session.get(Unit, unit_id).status == "consumed"


def test_existing_output_is_rejected_before_any_actions(tmp_path, monkeypatch):
    output = tmp_path / ".env"
    output.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["validate_e2_flow", "--output", str(output)])
    with pytest.raises(SystemExit) as exc:
        module.arguments()
    assert exc.value.code == 2
    assert output.read_text(encoding="utf-8") == "preserve"
