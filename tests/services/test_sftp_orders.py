from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.purchase_orders_exceptions import PurchaseOrdersHTTPError
from app.clients.sftp_orders import SFTPOrderFile, SFTPOrdersError
from app.models import (
    InboundEvent,
    InboundEventSource,
    InboundEventStatus,
    Order,
    OrderItem,
    OrderSource,
)
from app.services.sftp_orders import SFTPOrderIngestionService
from tests.support.farma_central import make_settings
from tests.support.orders import EXTERNAL_ORDER_ID, make_purchase_order


def order_file(path="/order.xml", order_id=EXTERNAL_ORDER_ID):
    return SFTPOrderFile(
        path=path,
        content=(
            f"<order><id>{order_id}</id><sku>UNTRUSTED</sku><qty>999999</qty></order>"
        ).encode(),
    )


class FakeSFTP:
    def __init__(self, *files):
        self.files = list(files)
        self.skip_paths = []
        self.error = None

    async def fetch_files(self, *, skip_paths):
        self.skip_paths.append(skip_paths)
        if self.error:
            raise self.error
        return [file for file in self.files if file.path not in skip_paths]


class FakeOrders:
    def __init__(self, detail=None):
        self.detail = detail or make_purchase_order()
        self.requested_ids = []
        self.decisions = []
        self.error = None

    async def get_order(self, order_id):
        self.requested_ids.append(order_id)
        if self.error:
            raise self.error
        return self.detail

    async def update_order_state(self, order_id, state):
        self.decisions.append((order_id, state))
        self.detail.state = state


def make_service(session, sftp, orders):
    return SFTPOrderIngestionService(sftp, orders, session, settings=make_settings())


@pytest.mark.anyio
async def test_imports_official_details_and_does_not_reread_the_same_file(db_session):
    sftp = FakeSFTP(order_file())
    orders = FakeOrders()
    service = make_service(db_session, sftp, orders)

    first = await service.poll()
    second = await service.poll()

    assert (first.received, first.processed, first.failed) == (1, 1, 0)
    assert (second.received, second.processed, second.failed) == (0, 0, 0)
    assert sftp.skip_paths == [set(), {"/order.xml"}]
    assert orders.requested_ids == [EXTERNAL_ORDER_ID]
    order = db_session.scalars(select(Order)).one()
    assert order.source is OrderSource.SFTP
    assert order.external_customer_id == "farmacia-externa"
    assert order.buyer_name == "Farmacia externa"
    assert order.buyer_email is None
    assert order.items[0].sku == "KIT-ANALGESIA"
    assert order.items[0].quantity == 2
    assert order.items[0].unit_price == 1500
    assert order.total == 3000
    assert order.delivery_deadline == orders.detail.delivery_deadline
    event = db_session.scalars(select(InboundEvent)).one()
    assert event.order_id == order.id
    assert event.status is InboundEventStatus.PROCESSED
    assert event.processed_at is not None
    assert event.attempt_count == 1


@pytest.mark.anyio
async def test_two_files_for_one_oc_link_to_one_local_order(db_session):
    sftp = FakeSFTP(order_file("/first.xml"), order_file("/second.xml"))
    orders = FakeOrders()
    result = await make_service(db_session, sftp, orders).poll()

    assert result.processed == 2
    order = db_session.scalars(select(Order)).one()
    assert len(order.inbound_events) == 2
    assert len(order.items) == 1


@pytest.mark.anyio
async def test_bad_xml_does_not_stop_good_files_and_is_not_retried(db_session):
    sftp = FakeSFTP(SFTPOrderFile("/bad.xml", b"<broken>"), order_file())
    orders = FakeOrders()
    service = make_service(db_session, sftp, orders)
    first = await service.poll()
    second = await service.poll()

    assert (first.processed, first.failed) == (1, 1)
    assert (second.processed, second.failed) == (0, 0)
    bad = db_session.scalar(
        select(InboundEvent).where(InboundEvent.external_event_id == "/bad.xml")
    )
    assert bad.status is InboundEventStatus.FAILED
    assert bad.last_error
    assert bad.next_retry_at is None
    assert orders.requested_ids == [EXTERNAL_ORDER_ID]


@pytest.mark.anyio
async def test_api_failure_retries_from_database_without_rereading_file(db_session):
    sftp = FakeSFTP(order_file())
    orders = FakeOrders()
    orders.error = PurchaseOrdersHTTPError(503)
    service = make_service(db_session, sftp, orders)
    result = await service.poll()
    event = db_session.scalars(select(InboundEvent)).one()

    assert result.failed == 1
    assert event.status is InboundEventStatus.FAILED
    assert event.next_retry_at > datetime.now(UTC)
    assert not db_session.scalars(select(Order)).all()
    assert (await service.poll()).processed == 0
    assert len(orders.requested_ids) == 1

    event.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    orders.error = None
    result = await make_service(db_session, sftp, orders).poll()

    assert (result.received, result.processed) == (0, 1)
    db_session.refresh(event)
    assert event.status is InboundEventStatus.PROCESSED
    assert event.attempt_count == 2
    assert event.last_error is None
    assert event.next_retry_at is None


@pytest.mark.anyio
async def test_respects_retry_after_in_rate_limit_response(db_session):
    orders = FakeOrders()
    orders.error = PurchaseOrdersHTTPError(429, retry_after_seconds=120)
    before = datetime.now(UTC)
    result = await make_service(db_session, FakeSFTP(order_file()), orders).poll()
    event = db_session.scalars(select(InboundEvent)).one()
    assert event.next_retry_at >= before + timedelta(seconds=120)
    assert result.retry_after_seconds == 120


@pytest.mark.anyio
async def test_rate_limit_pauses_batch_without_failing_the_other_events(db_session):
    orders = FakeOrders()
    orders.error = PurchaseOrdersHTTPError(429, retry_after_seconds=120)
    sftp = FakeSFTP(order_file("/first.xml"), order_file("/second.xml"))
    result = await make_service(db_session, sftp, orders).poll()
    assert result.failed == 1
    assert len(orders.requested_ids) == 1
    states = list(db_session.scalars(select(InboundEvent.status)))
    assert sorted(states) == sorted(
        [InboundEventStatus.PENDING, InboundEventStatus.FAILED]
    )


@pytest.mark.anyio
async def test_permanent_api_error_is_recorded_without_an_automatic_retry(db_session):
    orders = FakeOrders()
    orders.error = PurchaseOrdersHTTPError(404)
    await make_service(db_session, FakeSFTP(order_file()), orders).poll()
    event = db_session.scalars(select(InboundEvent)).one()
    assert event.status is InboundEventStatus.FAILED
    assert event.next_retry_at is None


@pytest.mark.anyio
async def test_stale_file_listing_does_not_overwrite_an_existing_event(db_session):
    class StaleListing(FakeSFTP):
        async def fetch_files(self, *, skip_paths):
            return self.files

    sftp = StaleListing(order_file())
    orders = FakeOrders()
    service = make_service(db_session, sftp, orders)
    await service.poll()
    original_payload = dict(db_session.scalars(select(InboundEvent)).one().payload)
    sftp.files = [SFTPOrderFile("/order.xml", b"changed contents")]
    result = await service.poll()
    assert (result.received, result.processed) == (0, 0)
    assert db_session.scalars(select(InboundEvent)).one().payload == original_payload
    assert orders.requested_ids == [EXTERNAL_ORDER_ID]


@pytest.mark.anyio
async def test_corrupt_persisted_payload_does_not_stop_other_events(db_session):
    db_session.add(
        InboundEvent(
            source=InboundEventSource.SFTP,
            external_event_id="/corrupt.xml",
            payload={"xml_base64": "###"},
        )
    )
    db_session.commit()
    result = await make_service(db_session, FakeSFTP(order_file()), FakeOrders()).poll()
    assert result.failed == 1
    assert result.processed == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"proveedor": "4"}, "not the supplier"),
        ({"precioUnitario": 1500.25}, "whole-peso"),
    ],
)
async def test_rejects_wrong_supplier_or_unrepresentable_prices(
    db_session, overrides, message
):
    orders = FakeOrders(make_purchase_order(**overrides))
    result = await make_service(db_session, FakeSFTP(order_file()), orders).poll()
    event = db_session.scalars(select(InboundEvent)).one()
    assert result.failed == 1
    assert message in event.last_error
    assert event.next_retry_at is None
    assert not db_session.scalars(select(Order)).all()


@pytest.mark.anyio
async def test_crash_rolls_back_processing_but_keeps_received_event(db_session):
    sftp = FakeSFTP(order_file())
    orders = FakeOrders()
    orders.error = RuntimeError("Simulated process crash")
    with pytest.raises(RuntimeError, match="process crash"):
        await make_service(db_session, sftp, orders).poll()
    event = db_session.scalars(select(InboundEvent)).one()
    assert event.status is InboundEventStatus.PENDING
    assert event.order_id is None
    assert event.processed_at is None

    orders.error = None
    sftp.error = SFTPOrdersError("Inbox temporarily unavailable")
    result = await make_service(db_session, sftp, orders).poll()
    assert result.connection_failed
    assert result.processed == 1
    assert db_session.scalars(select(Order)).one().id == event.order_id


@pytest.mark.anyio
async def test_does_not_overwrite_an_order_already_imported_by_another_channel(
    db_session,
):
    from tests.support.orders import make_external_order

    order = make_external_order(source=OrderSource.PUBSUB)
    db_session.add(order)
    db_session.commit()
    result = await make_service(db_session, FakeSFTP(order_file()), FakeOrders()).poll()

    assert result.processed == 1
    assert db_session.scalars(select(Order)).one().id == order.id
    assert order.source is OrderSource.PUBSUB
    assert len(order.items) == 1


@pytest.mark.anyio
async def test_received_event_survives_closing_session_before_processing(test_engine):
    suffix = uuid4().hex
    external_id = suffix[:24]
    path = f"/{suffix}.xml"
    sftp = FakeSFTP(order_file(path, external_id))
    detail = make_purchase_order(id=external_id)

    class InspectingOrders(FakeOrders):
        async def get_order(self, order_id):
            with Session(test_engine) as observer:
                event = observer.scalar(
                    select(InboundEvent).where(InboundEvent.external_event_id == path)
                )
                assert event is not None
                assert event.status is InboundEventStatus.PENDING
            return await super().get_order(order_id)

    orders = InspectingOrders(detail)
    orders.error = RuntimeError("Restart before processing")
    try:
        with Session(test_engine) as session, pytest.raises(RuntimeError):
            await make_service(session, sftp, orders).poll()
        orders.error = None
        with Session(test_engine) as session:
            result = await make_service(session, sftp, orders).poll()
            assert (result.received, result.processed) == (0, 1)
        with Session(test_engine) as session:
            event = session.scalar(
                select(InboundEvent).where(InboundEvent.external_event_id == path)
            )
            assert event.order.external_order_id == external_id
            assert event.status is InboundEventStatus.PROCESSED
    finally:
        with Session(test_engine) as session:
            event = session.scalar(
                select(InboundEvent).where(InboundEvent.external_event_id == path)
            )
            if event is not None:
                session.delete(event)
                session.flush()
            order = session.scalar(
                select(Order).where(Order.external_order_id == external_id)
            )
            if order is not None:
                session.delete(order)
            session.commit()


@pytest.mark.anyio
async def test_database_failure_does_not_leave_a_partial_order(db_session, monkeypatch):
    import app.services.order_ingestion as module

    original = module.upsert_external_order

    def interrupted_import(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Crash after creating order")

    monkeypatch.setattr(module, "upsert_external_order", interrupted_import)
    with pytest.raises(RuntimeError):
        await make_service(db_session, FakeSFTP(order_file()), FakeOrders()).poll()
    assert not db_session.scalars(select(Order)).all()
    assert not db_session.scalars(select(OrderItem)).all()
    event = db_session.scalars(select(InboundEvent)).one()
    assert event.status is InboundEventStatus.PENDING
