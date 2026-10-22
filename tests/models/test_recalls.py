from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    ExternalOrderStatus,
    Location,
    Lot,
    LotOrigin,
    Order,
    OrderItem,
    OrderSource,
    OrderStatus,
    OrderUnit,
    Product,
    ProductCategory,
    Recall,
    RecallDelivery,
    RecallLot,
    RecallNotification,
    RecallNotificationStatus,
    RecallOrder,
    RecallStatus,
    RecallUnit,
    Unit,
)


def create_inventory(db_session):
    suffix = uuid4().hex
    product = Product(
        sku=f"KIT-RECALL-{suffix}",
        name="Kit sujeto a retiro",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    location = Location(
        code=f"BODEGA-{suffix}",
        name="Bodega",
        is_refrigerated=False,
    )
    db_session.add_all([product, location])
    db_session.flush()

    expiry = datetime.now(UTC) + timedelta(days=30)
    lots = [
        Lot(
            external_lot_id=f"LOT-{kind}-{suffix}",
            product_id=product.id,
            expires_at=expiry,
            origin=LotOrigin.OWN_PRODUCTION,
        )
        for kind in ("ROOT", "DERIVED", "UNRELATED")
    ]
    db_session.add_all(lots)
    db_session.flush()

    units = [
        Unit(
            external_unit_id=f"UNIT-{kind}-{suffix}",
            lot_id=lot.id,
            current_location_id=location.id,
            status="available",
            effective_expires_at=expiry,
        )
        for kind, lot in zip(("STOCK", "DELIVERED", "UNRELATED"), lots, strict=True)
    ]
    db_session.add_all(units)
    db_session.flush()
    return product, location, lots, units


def create_recall(product, root_lot):
    issued_at = datetime.now(UTC)
    return Recall(
        recall_id=f"recall-{uuid4().hex}",
        origin_lot_id=root_lot.id,
        batch=root_lot.external_lot_id,
        sku=product.sku,
        reason="Desviación de calidad",
        issued_at=issued_at,
        deadline=issued_at + timedelta(minutes=30),
    )


def test_recall_preserves_complete_investigation_and_action_audit(db_session):
    product, location, lots, units = create_inventory(db_session)
    root_lot, derived_lot, _ = lots
    stock_unit, delivered_unit, _ = units
    delivered_unit.status = "dispatched"

    order = Order(
        buyer_name="Farmacia externa",
        external_order_id="64abc123def456789012abcd",
        external_customer_id="farmacia-externa",
        source=OrderSource.SFTP,
        status=OrderStatus.DISPATCHED,
        external_status=ExternalOrderStatus.FULFILLED,
        total=1500,
        items=[OrderItem(sku=product.sku, quantity=1, unit_price=1500)],
    )
    db_session.add(order)
    db_session.flush()
    assignment = OrderUnit(
        order_item_id=order.items[0].id,
        unit_id=delivered_unit.id,
        dispatched_at=datetime.now(UTC),
    )
    db_session.add(assignment)

    recall = create_recall(product, root_lot)
    db_session.add(recall)
    db_session.flush()
    root_record = RecallLot(recall_id=recall.id, lot_id=root_lot.id, generation=0)
    db_session.add(root_record)
    db_session.flush()
    derived_record = RecallLot(
        recall_id=recall.id,
        lot_id=derived_lot.id,
        parent_id=root_record.id,
        generation=1,
    )
    db_session.add(derived_record)
    db_session.flush()

    affected_unit = RecallUnit(
        recall_id=recall.id,
        lot_id=root_lot.id,
        unit_id=stock_unit.id,
        original_location_id=location.id,
        quarantined_at=datetime.now(UTC),
    )
    affected_order = RecallOrder(
        recall_id=recall.id,
        order_id=order.id,
        customer_reference=order.external_customer_id,
    )
    db_session.add_all([affected_unit, affected_order])
    db_session.flush()
    delivery = RecallDelivery(
        recall_id=recall.id,
        recall_order_id=affected_order.id,
        lot_id=derived_lot.id,
        unit_id=delivered_unit.id,
        order_unit_id=assignment.id,
        delivered_at=assignment.dispatched_at,
    )
    sent_at = datetime.now(UTC)
    notification = RecallNotification(
        recall_order_id=affected_order.id,
        status=RecallNotificationStatus.SENT,
        payload={"orderId": order.external_order_id},
        response={"accepted": True},
        attempt_count=1,
        sent_at=sent_at,
    )
    recall.status = RecallStatus.COMPLETED
    recall.reported_at = sent_at
    recall.last_report = {"affectedUnits": 2, "affectedOrders": 1}
    db_session.add_all([delivery, notification])
    db_session.flush()
    db_session.expire_all()

    saved = db_session.get(Recall, recall.id)
    assert saved.status is RecallStatus.COMPLETED
    assert saved.origin_lot.external_lot_id == root_lot.external_lot_id
    assert {record.lot_id for record in saved.lots} == {
        root_lot.id,
        derived_lot.id,
    }
    assert saved.units[0].unit_id == stock_unit.id
    assert saved.units[0].original_location_id == location.id
    assert saved.orders[0].deliveries[0].unit_id == delivered_unit.id
    assert saved.orders[0].notification.sent_at == sent_at
    assert saved.last_report == {"affectedUnits": 2, "affectedOrders": 1}


def test_unit_cannot_be_attached_to_an_unrelated_affected_lot(db_session):
    product, location, lots, units = create_inventory(db_session)
    root_lot, _, unrelated_lot = lots
    _, _, unrelated_unit = units
    recall = create_recall(product, root_lot)
    db_session.add(recall)
    db_session.flush()
    db_session.add(RecallLot(recall_id=recall.id, lot_id=root_lot.id, generation=0))
    db_session.flush()

    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(
            RecallUnit(
                recall_id=recall.id,
                lot_id=root_lot.id,
                unit_id=unrelated_unit.id,
                original_location_id=location.id,
            )
        )
        db_session.flush()

    assert error.value.orig.diag.constraint_name == "fk_recall_units_unit_lot"
    assert unrelated_unit.lot_id == unrelated_lot.id


def test_derived_lot_parent_must_belong_to_same_recall(db_session):
    product, _, lots, _ = create_inventory(db_session)
    first = create_recall(product, lots[0])
    second = create_recall(product, lots[1])
    db_session.add_all([first, second])
    db_session.flush()
    first_root = RecallLot(recall_id=first.id, lot_id=lots[0].id, generation=0)
    db_session.add(first_root)
    db_session.flush()

    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(
            RecallLot(
                recall_id=second.id,
                lot_id=lots[1].id,
                parent_id=first_root.id,
                generation=1,
            )
        )
        db_session.flush()

    assert error.value.orig.diag.constraint_name == "fk_recall_lots_parent_same_recall"


def test_recall_id_is_unique_and_deadline_must_follow_issue(db_session):
    product, _, lots, _ = create_inventory(db_session)
    recall = create_recall(product, lots[0])
    db_session.add(recall)
    db_session.flush()

    duplicate = create_recall(product, lots[0])
    duplicate.recall_id = recall.recall_id
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(duplicate)
        db_session.flush()

    invalid = create_recall(product, lots[0])
    invalid.deadline = invalid.issued_at
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(invalid)
        db_session.flush()
    assert error.value.orig.diag.constraint_name == "ck_recalls_deadline"
