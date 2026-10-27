import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ExternalOrderStatus,
    InboundEventStatus,
    Location,
    Lot,
    LotOrigin,
    MessagingDecision,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductCategory,
    ProductionRun,
    Unit,
)
from app.services.messaging import MessagingProcessingService
from app.services.orders import (
    OrderProcessingService,
    PubSubOrderIngestionService,
    SFTPOrderIngestionService,
)
from tests.services.test_messaging_processing import (
    FakeInterpreter,
    FakeMessaging,
    add_event,
)
from tests.services.test_pubsub_orders import FakePubSub, message
from tests.services.test_recalls import recall_database as recall_database
from tests.services.test_sftp_orders import FakeSFTP, order_file
from tests.support.farma_central import make_settings
from tests.support.orders import make_external_order, make_purchase_order


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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("elapsed", "deadline_after", "wait_seconds", "pending"),
    [
        (0, 7200, 3600, True),
        (3599, 7200, 3600, True),
        (3600, 7200, 3600, False),
        (599, 900, 3600, True),
        (600, 900, 3600, False),
        (0, 300, 3600, False),
        (0, -1, 3600, False),
        (0, 7200, 0, False),
    ],
)
async def test_stock_wait_is_bounded_by_age_and_dispatch_margin(
    db_session, elapsed, deadline_after, wait_seconds, pending
):
    created = datetime.now(UTC)
    order = add_order(
        db_session,
        "KIT-WAIT",
        1,
        created + timedelta(seconds=deadline_after),
        "1" * 24,
    )
    order.created_at = created
    db_session.flush()
    official = FakeOrders()
    service = OrderProcessingService(
        official,
        db_session,
        settings=make_settings(external_order_stock_wait_seconds=wait_seconds),
    )

    await service.process_order(order.id, now=created + timedelta(seconds=elapsed))

    assert order.status is (OrderStatus.RECEIVED if pending else OrderStatus.REJECTED)
    assert order.external_status is (
        ExternalOrderStatus.CREATED if pending else ExternalOrderStatus.REJECTED
    )
    if pending:
        assert order.decision_at is None
        assert "Waiting until" in order.decision_reason
        assert "capacity 0" in order.decision_reason
        assert official.decisions == []
    else:
        assert official.decisions == [(order.external_order_id, "rechazada")]


@pytest.mark.anyio
@pytest.mark.parametrize("capacity_source", ["stock", "production"])
async def test_waiting_order_is_accepted_once_capacity_appears(
    db_session, capacity_source
):
    now = datetime.now(UTC)
    product = add_product(db_session, "KIT-WAIT")
    order = add_order(db_session, product.sku, 1, now + timedelta(hours=2), "1" * 24)
    settings = make_settings(external_order_stock_wait_seconds=3600)
    official = FakeOrders()
    service = OrderProcessingService(official, db_session, settings=settings)
    await service.process_order(order.id, now=now)
    assert order.status is OrderStatus.RECEIVED
    db_session.commit()

    if capacity_source == "stock":
        add_stock(db_session, product, 1, now + timedelta(days=1))
    else:
        db_session.add(
            ProductionRun(
                expected_sku=product.sku,
                expected_quantity=1,
                requested_at=now,
                available_at=now + timedelta(minutes=5),
            )
        )
        db_session.flush()
    restarted = OrderProcessingService(official, db_session, settings=settings)
    await restarted.process_order(order.id, now=now + timedelta(minutes=2))
    await restarted.process_order(order.id, now=now + timedelta(minutes=3))

    assert order.status is OrderStatus.ACCEPTED
    assert order.decision_at == now + timedelta(minutes=2)
    assert official.decisions == [(order.external_order_id, "aceptada")]


@pytest.mark.anyio
async def test_restart_does_not_extend_stock_wait(recall_database):
    now = datetime.now(UTC)
    settings = make_settings(external_order_stock_wait_seconds=3600)
    official = FakeOrders()
    with Session(recall_database) as session:
        order = add_order(session, "KIT-WAIT", 1, now + timedelta(hours=2), "1" * 24)
        order.created_at = now
        order_id = order.id
        await OrderProcessingService(
            official, session, settings=settings
        ).process_order(order_id, now=now + timedelta(minutes=59))
        assert order.status is OrderStatus.RECEIVED
        session.commit()

    with Session(recall_database) as session:
        result = await OrderProcessingService(
            official, session, settings=settings
        ).process_order(order_id, now=now + timedelta(hours=1))
        assert result.status is OrderStatus.REJECTED
        assert result.created_at == now
        assert official.decisions == [(result.external_order_id, "rechazada")]


def test_waiting_orders_cannot_commit_the_same_stock_concurrently(recall_database):
    settings = make_settings(external_order_stock_wait_seconds=3600)
    official = FakeOrders()
    now = datetime.now(UTC)
    with Session(recall_database) as session:
        product = add_product(session, "KIT-SHARED")
        order_ids = []
        for digit in ("1", "2"):
            order = add_order(
                session, product.sku, 1, now + timedelta(hours=2), digit * 24
            )
            asyncio.run(
                OrderProcessingService(
                    official, session, settings=settings
                ).process_order(order.id)
            )
            assert order.status is OrderStatus.RECEIVED
            order_ids.append(order.id)
        add_stock(session, product, 1, now + timedelta(days=1))
        session.commit()

    ready = Barrier(2)

    def decide(order_id):
        with Session(recall_database) as session:
            ready.wait(timeout=5)
            result = asyncio.run(
                OrderProcessingService(
                    official, session, settings=settings
                ).process_order(order_id)
            )
            status = result.status
            session.commit()
            return status

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(decide, order_id) for order_id in order_ids]
        statuses = [future.result(timeout=10) for future in futures]

    assert sorted(statuses) == sorted([OrderStatus.ACCEPTED, OrderStatus.RECEIVED])
    assert len(official.decisions) == 1
    assert official.decisions[0][1] == "aceptada"


@pytest.mark.anyio
@pytest.mark.parametrize("channel", ["sftp", "pubsub", "messaging"])
async def test_each_channel_imports_order_without_rejecting_during_stock_wait(
    db_session, channel
):
    settings = make_settings(external_order_stock_wait_seconds=3600)
    official = AsyncMock()
    official.get_order.return_value = make_purchase_order(
        sku="KIT-RESP-ADULTO", vencimiento=datetime.now(UTC) + timedelta(hours=2)
    )
    if channel == "messaging":
        incoming = add_event(db_session, decision=MessagingDecision.ORDER)
        messaging = FakeMessaging()
        interpreter = FakeInterpreter()
        result = await MessagingProcessingService(
            messaging, interpreter, official, db_session, settings=settings
        ).process_pending()
        assert len(messaging.orders) == 1
        assert interpreter.calls == 0
        assert incoming.responded_at is not None
        assert incoming.status is InboundEventStatus.PROCESSED
    else:
        service = (
            SFTPOrderIngestionService(
                FakeSFTP(order_file()), official, db_session, settings=settings
            )
            if channel == "sftp"
            else PubSubOrderIngestionService(
                FakePubSub(message()), official, db_session, settings=settings
            )
        )
        result = await service.poll()
    assert result.processed == 1 and result.failed == 0
    order = db_session.scalars(select(Order)).one()
    assert order.status is OrderStatus.RECEIVED
    assert order.external_status is ExternalOrderStatus.CREATED
    assert order.decision_at is None
    assert "Waiting until" in order.decision_reason
    official.update_order_state.assert_not_awaited()
