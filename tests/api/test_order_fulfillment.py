from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
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
    Unit,
)
from app.queries.catalog import list_sellable_catalog
from app.queries.inventory import list_available_inventory


def _add_stock(
    session: Session,
    *,
    sku: str,
    days_until_expiration: list[int],
    sellable: bool = True,
) -> list[Unit]:
    suffix = uuid4().hex
    product = Product(
        sku=sku,
        name=f"Kit fulfillment {suffix}",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    location = Location(
        code=f"FULFILLMENT-{suffix}",
        name="Bodega fulfillment",
        is_refrigerated=False,
        is_sellable=sellable,
    )
    session.add_all([product, location])
    session.flush()

    lot = Lot(
        external_lot_id=f"FULFILLMENT-LOT-{suffix}",
        product_id=product.id,
        expires_at=datetime.now(UTC) + timedelta(days=120),
        origin=LotOrigin.OWN_PRODUCTION,
    )
    session.add(lot)
    session.flush()

    units = [
        Unit(
            external_unit_id=f"FULFILLMENT-UNIT-{suffix}-{index}",
            lot_id=lot.id,
            current_location_id=location.id,
            status="available",
            effective_expires_at=datetime.now(UTC) + timedelta(days=days),
        )
        for index, days in enumerate(days_until_expiration)
    ]
    session.add_all(units)
    session.flush()
    return units


def _add_order(
    session: Session,
    *,
    sku: str,
    quantity: int,
    status: OrderStatus = OrderStatus.PAID,
) -> Order:
    order = Order(
        buyer_name="Grace Hopper",
        buyer_email="grace@example.com",
        source=OrderSource.WEB,
        status=status,
        total=quantity * 1000,
        items=[OrderItem(sku=sku, quantity=quantity, unit_price=1000)],
    )
    session.add(order)
    session.flush()
    return order


def test_fulfillment_assigns_fefo_units_and_reserves_them(
    api_client: TestClient,
    db_session: Session,
) -> None:
    sku = f"KIT-FEFO-{uuid4().hex}"
    units = _add_stock(
        db_session,
        sku=sku,
        days_until_expiration=[30, 10, 20, -1],
    )
    order = _add_order(db_session, sku=sku, quantity=2)

    response = api_client.post(f"/api/orders/{order.id}/fulfillment")

    assert response.status_code == 200
    assigned_ids = {
        assigned["external_unit_id"]
        for assigned in response.json()["items"][0]["assigned_units"]
    }
    assert assigned_ids == {units[1].external_unit_id, units[2].external_unit_id}
    db_session.expire_all()
    assert db_session.get(Unit, units[1].id).status == "reserved"
    assert db_session.get(Unit, units[2].id).status == "reserved"
    assert db_session.get(Unit, units[0].id).status == "available"


def test_fulfillment_is_idempotent(
    api_client: TestClient,
    db_session: Session,
) -> None:
    sku = f"KIT-IDEMPOTENT-{uuid4().hex}"
    _add_stock(db_session, sku=sku, days_until_expiration=[10, 20])
    order = _add_order(db_session, sku=sku, quantity=1)

    first = api_client.post(f"/api/orders/{order.id}/fulfillment")
    second = api_client.post(f"/api/orders/{order.id}/fulfillment")

    assert first.status_code == 200
    assert second.status_code == 200
    assert (
        first.json()["items"][0]["assigned_units"]
        == second.json()["items"][0]["assigned_units"]
    )
    assert len(db_session.scalars(select(OrderUnit)).all()) == 1


def test_assigned_unit_stays_out_of_stock_if_external_sync_marks_it_available(
    api_client: TestClient,
    db_session: Session,
) -> None:
    sku = f"KIT-RESERVED-{uuid4().hex}"
    _add_stock(db_session, sku=sku, days_until_expiration=[10, 20])
    order = _add_order(db_session, sku=sku, quantity=1)
    response = api_client.post(f"/api/orders/{order.id}/fulfillment")
    assigned_unit_id = UUID(response.json()["items"][0]["assigned_units"][0]["unit_id"])

    assigned_unit = db_session.get(Unit, assigned_unit_id)
    assert assigned_unit is not None
    assigned_unit.status = "available"
    db_session.flush()

    catalog_item = next(
        item
        for item in list_sellable_catalog(db_session, as_of=datetime.now(UTC))
        if item.sku == sku
    )
    available_units = list_available_inventory(db_session, sku=sku)

    assert catalog_item.stock == 1
    assert assigned_unit.id not in {unit.id for unit in available_units}


def test_fulfillment_rejects_unpaid_order(
    api_client: TestClient,
    db_session: Session,
) -> None:
    sku = f"KIT-UNPAID-{uuid4().hex}"
    _add_stock(db_session, sku=sku, days_until_expiration=[10])
    order = _add_order(
        db_session,
        sku=sku,
        quantity=1,
        status=OrderStatus.PENDING_PAYMENT,
    )
    db_session.commit()

    response = api_client.post(f"/api/orders/{order.id}/fulfillment")

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"Order {order.id} must be paid before fulfillment"
    )
    assert db_session.scalar(select(OrderUnit.id)) is None


def test_insufficient_stock_does_not_leave_partial_assignments(
    api_client: TestClient,
    db_session: Session,
) -> None:
    sku = f"KIT-SHORTAGE-{uuid4().hex}"
    units = _add_stock(db_session, sku=sku, days_until_expiration=[10])
    order = _add_order(db_session, sku=sku, quantity=2)
    unit_id = units[0].id
    db_session.commit()

    response = api_client.post(f"/api/orders/{order.id}/fulfillment")

    assert response.status_code == 409
    assert response.json()["detail"] == f"Insufficient stock to fulfill {sku}"
    db_session.expire_all()
    assert db_session.get(Unit, unit_id).status == "available"
    assert db_session.scalar(select(OrderUnit.id)) is None


def test_one_unit_cannot_be_assigned_to_two_orders(
    api_client: TestClient,
    db_session: Session,
) -> None:
    sku = f"KIT-EXCLUSIVE-{uuid4().hex}"
    units = _add_stock(db_session, sku=sku, days_until_expiration=[10])
    first_order = _add_order(db_session, sku=sku, quantity=1)
    second_order = _add_order(db_session, sku=sku, quantity=1)

    first = api_client.post(f"/api/orders/{first_order.id}/fulfillment")
    second = api_client.post(f"/api/orders/{second_order.id}/fulfillment")

    assert first.status_code == 200
    assert second.status_code == 409
    assignments = db_session.scalars(
        select(OrderUnit).where(OrderUnit.unit_id == units[0].id)
    ).all()
    assert len(assignments) == 1
    assert assignments[0].order_item.order_id == first_order.id
