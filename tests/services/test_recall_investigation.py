from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.recalls import RecallSnapshotConflictError
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    Recall,
    RecallDelivery,
    RecallLot,
    RecallNotification,
    RecallOrder,
    RecallStatus,
    RecallUnit,
)
from app.schemas.recalls import RecallWebhookPayload
from app.services.recalls import (
    RecallInvestigationError,
    RecallInvestigationService,
    RecallWebhookService,
)
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory


@pytest.fixture
def inventory(db_session):
    return RecallInventory(db_session)


def test_multigeneration_graph_finds_only_affected_lots_stock_and_deliveries(
    db_session, inventory
):
    g = inventory
    upstream, root, sibling, other = (
        g.lot(name, "RAW") for name in ("ancestor", "root", "sibling", "other")
    )
    a, b, merged, final, zero = (
        g.lot(name) for name in ("a", "b", "merge", "final", "zero")
    )
    g.link(root, upstream)
    g.link(sibling, upstream)
    g.link(a, root)
    g.link(b, root)
    consumed = g.unit("used-in-merge", a)  # Deliberately stale status.
    g.link(merged, a, consumed_unit=consumed)
    g.link(merged, b, other)
    g.link(final, merged)
    g.link(zero, root, quantity=0)
    g.link(None, root)
    root_stock = g.unit("root-stock", root, location="reception")
    root_reserved = g.unit("root-reserved", root, location="buffer")
    expired = g.unit("expired", b, expired=True)
    quarantined = g.unit(
        "quarantined", merged, status="quarantined", location="quarantine"
    )
    final_reserved = g.unit("final-reserved", final)
    g.unit("missing", final, status="unavailable")
    g.unit("already-consumed", root, status="consumed")
    g.unit("unrelated-stock", other)
    direct = g.unit("direct-delivery", root)
    final_delivery = g.unit("kit-delivery", final)
    merged_delivery = g.unit("merged-delivery", merged)
    retail = g.unit("web-delivery", b)
    unrelated_delivery = g.unit("unrelated-delivery", other)
    first_order = g.order((direct, True), (root_reserved, False))
    second_order = g.order(
        (final_delivery, True),
        (merged_delivery, True),
        (unrelated_delivery, True),
        (final_reserved, False),
    )
    retail_order = g.order((retail, True), external=False)
    direct.status = "available"  # Dispatch evidence takes precedence over sync status.
    recall = g.recall(root)
    db_session.commit()

    result = RecallInvestigationService(db_session).investigate(recall.recall_id)

    assert {lot.lot_id: lot.generation for lot in result.lots} == {
        root.id: 0,
        a.id: 1,
        b.id: 1,
        merged.id: 2,
        final.id: 3,
    }
    assert len(result.production_links) == 5
    assert {unit.unit_id for unit in result.stock_units} == {
        root_stock.id,
        root_reserved.id,
        expired.id,
        quarantined.id,
        final_reserved.id,
    }
    assert {unit.location_code for unit in result.stock_units} == {
        location.code for location in g.locations.values()
    }
    by_order = {order.order_id: order for order in result.orders}
    assert set(by_order) == {first_order.id, second_order.id, retail_order.id}
    assert {d.unit_id for d in by_order[second_order.id].deliveries} == {
        final_delivery.id,
        merged_delivery.id,
    }
    assert by_order[first_order.id].external_order_id == first_order.external_order_id
    assert by_order[retail_order.id].customer_reference == "cliente@example.test"
    assert len([d for order in result.orders for d in order.deliveries]) == 4
    assert db_session.scalar(select(func.count()).select_from(RecallLot)) == 5
    assert db_session.scalar(select(func.count()).select_from(RecallUnit)) == 5
    assert db_session.scalar(select(func.count()).select_from(RecallDelivery)) == 4
    assert recall.origin_lot_id == root.id
    assert recall.status == RecallStatus.INVESTIGATING
    assert root_stock.status == "available"
    assert quarantined.current_location_id == g.locations["quarantine"].id
    assert db_session.scalar(select(func.count()).select_from(RecallNotification)) == 0


def test_cycles_and_shared_descendants_terminate_without_duplicates(
    db_session, inventory
):
    root, a, b = (inventory.lot(name) for name in ("root", "a", "b"))
    inventory.link(a, root)
    inventory.link(b, a)
    inventory.link(root, b)
    recall = inventory.recall(root)
    db_session.commit()

    result = RecallInvestigationService(db_session).investigate(recall.recall_id)

    assert [(lot.lot_id, lot.generation) for lot in result.lots] == [
        (root.id, 0),
        (a.id, 1),
        (b.id, 2),
    ]
    assert len(result.production_links) == 3
    assert result.stock_units == result.orders == []


def test_reinvestigation_preserves_original_location_and_response_audit(
    db_session, inventory
):
    root = inventory.lot("root")
    unit = inventory.unit("stock", root, location="buffer")
    delivered = inventory.unit("delivery", root)
    inventory.order((delivered, True))
    recall = inventory.recall(root)
    db_session.commit()
    service = RecallInvestigationService(db_session)
    first = service.investigate(recall.recall_id)
    snapshot = db_session.scalars(select(RecallUnit)).one()
    snapshot_id = snapshot.id
    now = datetime.now(UTC)
    snapshot.quarantined_at = now
    unit.current_location = inventory.locations["quarantine"]
    unit.status = "quarantined"
    affected_order = db_session.scalars(select(RecallOrder)).one()
    notification = RecallNotification(
        recall_order_id=affected_order.id, payload={"orderId": "pending"}
    )
    db_session.add(notification)
    recall.status = RecallStatus.COMPLETED
    recall.reported_at = now
    recall.last_report = {"version": 1}
    db_session.commit()

    second = service.investigate(recall.recall_id)

    assert first.stock_units[0].location_code == inventory.locations["buffer"].code
    assert second.stock_units[0].location_code == inventory.locations["quarantine"].code
    assert (
        second.stock_units[0].original_location_code
        == inventory.locations["buffer"].code
    )
    saved = db_session.scalars(select(RecallUnit)).one()
    assert saved.id == snapshot_id and saved.quarantined_at == now
    assert recall.status == RecallStatus.COMPLETED
    assert recall.last_report == {"version": 1} and recall.reported_at == now
    assert db_session.scalar(select(func.count()).select_from(RecallDelivery)) == 1
    assert db_session.scalar(select(func.count()).select_from(RecallOrder)) == 1
    assert db_session.get(RecallNotification, notification.id) is not None


def test_webhook_event_is_linked_to_persistent_recall_during_investigation(
    db_session, inventory
):
    root = inventory.lot("root")
    now = inventory.now
    payload = RecallWebhookPayload(
        recallId="rcl-webhook",
        batch=root.external_lot_id,
        sku=root.product.sku,
        reason="Calidad",
        issuedAt=now,
        deadline=now + timedelta(minutes=30),
    )
    RecallWebhookService(db_session).receive(payload)

    result = RecallInvestigationService(db_session).investigate(payload.recall_id)

    saved = db_session.scalars(select(Recall)).one()
    incoming = db_session.scalars(select(InboundEvent)).one()
    assert saved.inbound_event_id == incoming.id
    assert saved.origin_lot_id == result.origin_lot_id == root.id
    assert incoming.source == InboundEventSource.RECALL
    assert (
        incoming.status == InboundEventStatus.PENDING
    )  # Response still belongs to step 17.


@pytest.mark.parametrize(
    "problem", ["unknown_recall", "unknown_batch", "wrong_sku", "wrong_origin"]
)
def test_invalid_origins_fail_without_creating_findings(db_session, inventory, problem):
    root = inventory.lot("root")
    recall = inventory.recall(root)
    if problem == "unknown_batch":
        recall.batch = "missing"
    if problem == "wrong_sku":
        recall.sku = "different"
    if problem == "wrong_origin":
        recall.origin_lot_id = inventory.lot("other").id
    db_session.commit()

    with pytest.raises(RecallInvestigationError):
        RecallInvestigationService(db_session).investigate(
            "missing" if problem == "unknown_recall" else recall.recall_id
        )

    assert db_session.scalar(select(func.count()).select_from(RecallLot)) == 0
    assert recall.status == RecallStatus.RECEIVED


def test_unrelated_saved_lot_is_not_silently_reported(db_session, inventory):
    root, unrelated = inventory.lot("root"), inventory.lot("other")
    recall = inventory.recall(root)
    db_session.add(RecallLot(recall_id=recall.id, lot_id=unrelated.id, generation=0))
    db_session.commit()
    with pytest.raises(RecallSnapshotConflictError, match="outside its lineage"):
        RecallInvestigationService(db_session).investigate(recall.recall_id)
    assert recall.origin_lot_id is None


def test_reinvestigation_adds_new_descendants_and_reclassifies_dispatched_stock(
    db_session, inventory
):
    root = inventory.lot("root")
    unit = inventory.unit("stock", root)
    recall = inventory.recall(root)
    db_session.commit()
    service = RecallInvestigationService(db_session)
    assert len(service.investigate(recall.recall_id).stock_units) == 1
    inventory.order((unit, True))
    child = inventory.lot("new-child")
    inventory.link(child, root)
    new_unit = inventory.unit("new-stock", child)
    db_session.commit()

    result = service.investigate(recall.recall_id)

    assert {lot.lot_id for lot in result.lots} == {root.id, child.id}
    assert [u.unit_id for u in result.stock_units] == [new_unit.id]
    assert result.orders[0].deliveries[0].unit_id == unit.id
    # Keep historical discovery rows; only the returned snapshot is current stock.
    assert db_session.scalar(select(func.count()).select_from(RecallUnit)) == 2


def test_missing_origin_can_be_retried_after_inventory_sync(db_session, inventory):
    root = inventory.lot("root")
    payload = RecallWebhookPayload(
        recallId="rcl-before-sync",
        batch="arriving-batch",
        sku=root.product.sku,
        reason="Calidad",
        issuedAt=inventory.now,
        deadline=inventory.now + timedelta(minutes=30),
    )
    RecallWebhookService(db_session).receive(payload)
    service = RecallInvestigationService(db_session)
    with pytest.raises(RecallInvestigationError, match="not synchronized"):
        service.investigate(payload.recall_id)
    assert db_session.scalar(select(func.count()).select_from(Recall)) == 0
    assert (
        db_session.scalars(select(InboundEvent)).one().status
        == InboundEventStatus.PENDING
    )
    root.external_lot_id = payload.batch
    db_session.commit()
    assert service.investigate(payload.recall_id).origin_lot_id == root.id


def test_concurrent_investigations_persist_each_finding_once(recall_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy.orm import Session

    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        root = inventory.lot("root")
        inventory.unit("stock", root)
        payload = RecallWebhookPayload(
            recallId="rcl-concurrent",
            batch=root.external_lot_id,
            sku=root.product.sku,
            reason="Calidad",
            issuedAt=inventory.now,
            deadline=inventory.now + timedelta(minutes=30),
        )
        RecallWebhookService(session).receive(payload)
    ready = Barrier(2)

    def investigate():
        with Session(recall_database) as session:
            ready.wait(timeout=5)
            return RecallInvestigationService(session).investigate(payload.recall_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(investigate), pool.submit(investigate)
        assert (
            first.result(timeout=10).stock_units
            == second.result(timeout=10).stock_units
        )
    with Session(recall_database) as session:
        assert session.scalar(select(func.count()).select_from(Recall)) == 1
        assert session.scalar(select(func.count()).select_from(RecallLot)) == 1
        assert session.scalar(select(func.count()).select_from(RecallUnit)) == 1
