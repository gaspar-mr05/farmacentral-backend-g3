from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import (
    ExternalOrderStatus,
    Location,
    Lot,
    LotOrigin,
    OrderItem,
    OrderStatus,
    Product,
    ProductCategory,
    ProductionRun,
    Unit,
)
from app.services.orders import OrderProcessingService
from tests.support.orders import make_external_order


class FakeOrders:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, str]] = []

    async def update_order_state(self, order_id: str, state: str) -> None:
        self.decisions.append((order_id, state))


def add_product(db_session, sku: str) -> Product:
    product = Product(
        sku=sku,
        name=sku,
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    db_session.add(product)
    db_session.flush()
    return product


def add_stock(
    db_session, product: Product, quantity: int, expires_at: datetime
) -> None:
    location = Location(
        code=f"SELLABLE-{uuid4().hex}",
        name="Sellable stock",
        is_sellable=True,
    )
    lot = Lot(
        external_lot_id=f"LOT-{uuid4().hex}",
        product=product,
        expires_at=expires_at,
        origin=LotOrigin.OWN_PRODUCTION,
    )
    db_session.add_all([location, lot])
    db_session.flush()
    db_session.add_all(
        Unit(
            external_unit_id=f"UNIT-{uuid4().hex}",
            lot=lot,
            current_location=location,
            status="available",
            effective_expires_at=expires_at,
        )
        for _ in range(quantity)
    )
    db_session.flush()


def add_order(
    db_session, sku: str, quantity: int, deadline: datetime, external_id: str
):
    order = make_external_order(
        external_order_id=external_id,
        delivery_deadline=deadline,
        total=quantity * 1500,
        items=[OrderItem(sku=sku, quantity=quantity, unit_price=1500)],
    )
    db_session.add(order)
    db_session.flush()
    return order


@pytest.mark.anyio
async def test_accepts_when_current_stock_covers_the_order(db_session):
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    deadline = now + timedelta(hours=2)
    product = add_product(db_session, "KIT-STOCK")
    add_stock(db_session, product, 2, deadline + timedelta(days=1))
    order = add_order(db_session, "KIT-STOCK", 2, deadline, "111111111111111111111111")
    orders = FakeOrders()

    result = await OrderProcessingService(orders, db_session).process_order(
        order.id, now=now
    )

    assert result.status is OrderStatus.ACCEPTED
    assert result.external_status is ExternalOrderStatus.ACCEPTED
    assert result.decision_at == now
    assert "stock 2" in result.decision_reason
    assert orders.decisions == [(order.external_order_id, "aceptada")]


@pytest.mark.anyio
async def test_rejects_an_order_after_its_deadline(db_session):
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    order = add_order(
        db_session,
        "KIT-LATE",
        1,
        now - timedelta(seconds=1),
        "222222222222222222222222",
    )
    orders = FakeOrders()

    result = await OrderProcessingService(orders, db_session).process_order(
        order.id, now=now
    )

    assert result.status is OrderStatus.REJECTED
    assert "deadline has passed" in result.decision_reason
    assert orders.decisions == [(order.external_order_id, "rechazada")]


@pytest.mark.anyio
async def test_accepts_only_production_already_confirmed_before_deadline(db_session):
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    deadline = now + timedelta(hours=2)
    add_product(db_session, "KIT-PRODUCTION")
    db_session.add(
        ProductionRun(
            requested_at=now,
            expected_sku="KIT-PRODUCTION",
            expected_quantity=2,
            available_at=now + timedelta(minutes=30),
        )
    )
    order = add_order(
        db_session,
        "KIT-PRODUCTION",
        2,
        deadline,
        "333333333333333333333333",
    )
    orders = FakeOrders()

    result = await OrderProcessingService(orders, db_session).process_order(
        order.id, now=now
    )

    assert result.status is OrderStatus.ACCEPTED
    assert "confirmed production 2" in result.decision_reason


@pytest.mark.anyio
async def test_existing_commitment_prevents_promising_the_same_stock_twice(db_session):
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    deadline = now + timedelta(hours=2)
    product = add_product(db_session, "KIT-COMMITTED")
    add_stock(db_session, product, 2, deadline + timedelta(days=1))
    first = add_order(
        db_session,
        "KIT-COMMITTED",
        2,
        deadline,
        "444444444444444444444444",
    )
    second = add_order(
        db_session,
        "KIT-COMMITTED",
        1,
        deadline,
        "555555555555555555555555",
    )
    orders = FakeOrders()
    service = OrderProcessingService(orders, db_session)

    await service.process_order(first.id, now=now)
    result = await service.process_order(second.id, now=now)

    assert result.status is OrderStatus.REJECTED
    assert "committed 2" in result.decision_reason


@pytest.mark.anyio
async def test_reprocessing_an_order_does_not_send_a_second_decision(db_session):
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    order = add_order(
        db_session,
        "KIT-IDEMPOTENT",
        1,
        now - timedelta(seconds=1),
        "666666666666666666666666",
    )
    orders = FakeOrders()
    service = OrderProcessingService(orders, db_session)

    first = await service.process_order(order.id, now=now)
    second = await service.process_order(order.id, now=now + timedelta(minutes=1))

    assert second.status is first.status is OrderStatus.REJECTED
    assert second.decision_at == now
    assert orders.decisions == [(order.external_order_id, "rechazada")]
