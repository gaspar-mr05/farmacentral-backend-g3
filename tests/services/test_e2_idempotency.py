"""Restart and duplicate-delivery scenarios with real commits and simulated APIs."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralHTTPError, FarmaCentralTimeoutError
from app.clients.messaging import MessagingAlreadyRespondedError, MessagingTimeoutError
from app.clients.pubsub import PubSubOrderMessage
from app.db.external_orders import InvalidExternalOrderError, upsert_external_order
from app.db.inbound_events import record_recall
from app.db.operation_locks import operation_lock
from app.models import (
    CustodyEvent,
    ExternalOrderStatus,
    InboundEvent,
    InboundEventStatus,
    MessagingDecision,
    Order,
    OrderSource,
    OrderStatus,
    RecallNotification,
    RecallNotificationStatus,
)
from app.schemas.recalls import RecallWebhookPayload
from app.services.messaging.processing import MessagingProcessingService
from app.services.messaging.recovery import MessagingRecoveryService
from app.services.orders import (
    OrderDispatchService,
    OrderFulfillmentService,
    OrderProcessingService,
)
from app.services.recalls import (
    RecallResponseError,
    RecallResponseService,
    RecallWorkerService,
)
from app.services.recalls.notification_recovery import (
    NotificationResolutionError,
    resolve_notification,
)
from app.services.recalls.response import RecallNotificationUncertainError
from tests.services.test_external_order_regressions import prepare_dispatch
from tests.services.test_messaging_processing import (
    FakeInterpreter,
    FakeMessaging,
    add_event,
)
from tests.services.test_messaging_processing import (
    FakeOrders as MessageOrders,
)
from tests.services.test_order_processing import (
    FakeOrders as DecisionOrders,
)
from tests.services.test_order_processing import (
    add_order,
    add_product,
    add_stock,
)
from tests.services.test_pubsub_orders import (
    FakePubSub,
    message,
)
from tests.services.test_pubsub_orders import (
    make_service as pubsub_service,
)
from tests.services.test_recalls import recall_database as recall_database
from tests.services.test_sftp_orders import (
    FakeSFTP,
    order_file,
)
from tests.services.test_sftp_orders import (
    make_service as sftp_service,
)
from tests.support.farma_central import make_settings
from tests.support.orders import EXTERNAL_ORDER_ID, make_purchase_order
from tests.support.recalls import RecallInventory


class ProcessStopped(BaseException):
    """Model process termination, bypassing normal application error handlers."""


@pytest.mark.anyio
@pytest.mark.parametrize("channel", ["sftp", "pubsub"])
@pytest.mark.parametrize("accepted", [False, True])
async def test_restart_after_official_decision_does_not_decide_twice(
    recall_database, channel, accepted
):
    now = datetime.now(UTC)

    class OfficialOrders:
        state = "creada"
        changes = []

        async def get_order(self, order_id):
            return make_purchase_order(
                estado=self.state, vencimiento=now + timedelta(hours=1)
            )

        async def update_order_state(self, order_id, state):
            self.changes.append((order_id, state))
            self.state = state
            raise ProcessStopped

    official = OfficialOrders()
    transport = FakeSFTP(order_file()) if channel == "sftp" else FakePubSub(message())
    factory = sftp_service if channel == "sftp" else pubsub_service
    with Session(recall_database) as session:
        if accepted:
            product = add_product(session, "KIT-ANALGESIA")
            add_stock(session, product, 2, now + timedelta(days=1))
            session.commit()
        with pytest.raises(ProcessStopped):
            await factory(session, transport, official).poll()

    if channel == "pubsub":
        transport.messages = [
            PubSubOrderMessage("message-1", "new-ack-id", message().data)
        ]
    with Session(recall_database) as session:
        event = session.scalars(select(InboundEvent)).one()
        assert event.status == InboundEventStatus.PENDING
        result = await factory(session, transport, official).poll()
        assert result.processed == 1
        order = session.scalars(select(Order)).one()
        assert order.status == (
            OrderStatus.ACCEPTED if accepted else OrderStatus.REJECTED
        )
        assert len(official.changes) == 1
        assert (await factory(session, transport, official).poll()).processed == 0
        assert session.scalar(select(func.count()).select_from(Order)) == 1


@pytest.mark.anyio
async def test_cached_order_cannot_send_a_second_acceptance(recall_database):
    with (
        Session(recall_database) as first,
        Session(recall_database, expire_on_commit=False) as stale,
    ):
        now = datetime.now(UTC)
        product = add_product(first, "KIT-STALE")
        add_stock(first, product, 1, now + timedelta(days=1))
        order = add_order(first, product.sku, 1, now + timedelta(hours=1), "1" * 24)
        first.commit()
        cached = stale.get(Order, order.id)
        assert cached.status == OrderStatus.RECEIVED
        official = DecisionOrders()
        await OrderProcessingService(official, first).process_order(order.id)
        first.commit()
        await OrderProcessingService(official, stale).process_order(order.id)
        stale.commit()
        assert official.decisions == [(order.external_order_id, "aceptada")]
        refreshed = upsert_external_order(
            stale,
            make_purchase_order(
                id=order.external_order_id,
                sku=product.sku,
                cantidad=1,
                vencimiento=order.delivery_deadline,
            ),
            source=OrderSource.PUBSUB,
            supplier_id="3",
        )
        assert refreshed.external_status == ExternalOrderStatus.ACCEPTED


def test_duplicate_cannot_rewrite_assigned_order_items(db_session):
    order, _, _, _ = prepare_dispatch(db_session)
    OrderFulfillmentService(db_session).fulfill(order.id)
    with pytest.raises(InvalidExternalOrderError, match="rewrite"):
        upsert_external_order(
            db_session,
            make_purchase_order(
                id=order.external_order_id,
                sku="OTHER-KIT",
                cantidad=1,
            ),
            source=OrderSource.PUBSUB,
            supplier_id="3",
        )


@pytest.mark.anyio
async def test_duplicate_of_fulfilled_order_never_reopens_or_dispatches_again(
    db_session,
):
    order, _, client, official = prepare_dispatch(db_session)
    OrderFulfillmentService(db_session).fulfill(order.id)
    await OrderDispatchService(client, db_session, official).dispatch(order.id)
    decisions = DecisionOrders()
    for state in ("cumplida", "aceptada", "creada"):
        upsert_external_order(
            db_session,
            make_purchase_order(
                id=order.external_order_id,
                sku=order.items[0].sku,
                cantidad=1,
                estado=state,
                vencimiento=order.delivery_deadline,
            ),
            source=OrderSource.PUBSUB,
            supplier_id="3",
        )
        await OrderProcessingService(decisions, db_session).process_order(order.id)
        assert order.status == OrderStatus.DISPATCHED
        assert order.external_status == ExternalOrderStatus.FULFILLED
        await OrderDispatchService(client, db_session, official).dispatch(order.id)
    assert len(client.dispatches) == 1
    assert decisions.decisions == []


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [False, True])
async def test_active_message_lock_survives_commits_and_blocks_stale_recovery(
    recall_database, expired
):
    with Session(recall_database) as owner:
        event = add_event(
            owner,
            received_at=datetime.now(UTC) - timedelta(minutes=31 if expired else 1),
        )
        event.status = InboundEventStatus.PROCESSING
        event.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
        owner.commit()
        event_id = event.id
        with operation_lock(owner, f"message:{event.external_event_id}") as acquired:
            assert acquired
            owner.commit()
            with Session(recall_database) as contender:
                messaging, interpreter = FakeMessaging(), FakeInterpreter()
                result = await MessagingProcessingService(
                    messaging,
                    interpreter,
                    MessageOrders(),
                    contender,
                    settings=make_settings(),
                ).process_pending()
                assert result.processed == result.failed == 0
                assert interpreter.calls == 0
                assert messaging.discards == []
        with Session(recall_database) as restarted:
            result = await MessagingProcessingService(
                FakeMessaging(),
                FakeInterpreter(),
                MessageOrders(),
                restarted,
                settings=make_settings(),
            ).process_pending()
            assert (result.failed if expired else result.processed) == 1
            assert restarted.get(InboundEvent, event_id).status == (
                InboundEventStatus.FAILED if expired else InboundEventStatus.PROCESSED
            )


@pytest.mark.anyio
async def test_interpretation_is_not_repeated_after_crash_before_response(
    recall_database,
):
    interpreter = FakeInterpreter()

    class BeforeSending(FakeMessaging):
        async def discard(self, message_id, *, reason):
            if self.error:
                raise self.error
            await super().discard(message_id, reason=reason)

    messaging = BeforeSending()
    messaging.error = ProcessStopped()
    with Session(recall_database) as session:
        event = add_event(session)
        with pytest.raises(ProcessStopped):
            await MessagingProcessingService(
                messaging,
                interpreter,
                MessageOrders(),
                session,
                settings=make_settings(),
            ).process_pending()
        event_id = event.id
    with Session(recall_database) as restarted:
        saved = restarted.get(InboundEvent, event_id)
        assert saved.decision is not None
        saved.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
        restarted.commit()
        messaging.error = None
        result = await MessagingProcessingService(
            messaging, interpreter, MessageOrders(), restarted, settings=make_settings()
        ).process_pending()
        assert result.processed == 1
        assert interpreter.calls == 1
        assert len(messaging.discards) == 1
        assert saved.status == InboundEventStatus.PROCESSED


@pytest.mark.anyio
async def test_slow_interpretation_cannot_respond_after_message_deadline(
    db_session, monkeypatch
):
    from app.services.messaging import processing as module

    received = datetime.now(UTC)
    current = [received]

    class Clock:
        @staticmethod
        def now(tz):
            return current[0]

    class SlowInterpreter:
        async def interpret(self, event):
            event.decision = MessagingDecision.DISCARDED
            event.reason = "No es un pedido"
            current[0] = received + timedelta(minutes=31)

    monkeypatch.setattr(module, "datetime", Clock)
    add_event(db_session, received_at=received)
    messaging = FakeMessaging()
    result = await MessagingProcessingService(
        messaging,
        SlowInterpreter(),
        MessageOrders(),
        db_session,
        settings=make_settings(),
    ).process_pending()
    assert result.failed == 1
    assert messaging.discards == []
    assert (
        db_session.scalars(select(InboundEvent)).one().decision
        == MessagingDecision.DISCARDED
    )


def recall_setup(session, *, stock=True, delivery=True):
    inventory = RecallInventory(session)
    root = inventory.lot("root")
    unit = inventory.unit("stock", root) if stock else None
    delivered = inventory.unit("delivered", root) if delivery else None
    order = inventory.order((delivered, True)) if delivered else None
    payload = {
        "recallId": f"rcl-{inventory.suffix}",
        "batch": root.external_lot_id,
        "sku": root.product.sku,
        "reason": "Calidad",
        "issuedAt": inventory.now,
        "deadline": inventory.now + timedelta(minutes=30),
    }
    client = AsyncMock()
    client.get_active_recalls.return_value = [payload]
    client.get_spaces.return_value = [
        {"_id": inventory.locations["quarantine"].code, "quarantine": True}
    ]
    client.get_space_products.return_value = []
    session.commit()
    return inventory, payload["recallId"], client, unit, order


@pytest.mark.anyio
async def test_recall_claim_remains_exclusive_after_commits_and_stale_timeout(
    recall_database,
):
    with Session(recall_database) as owner:
        _, recall_id, client, _, _ = recall_setup(owner)
        record_recall(
            owner,
            RecallWebhookPayload.model_validate(
                client.get_active_recalls.return_value[0]
            ),
        )
        owner.commit()
        event = owner.scalars(select(InboundEvent)).one()
        event.status = InboundEventStatus.PROCESSING
        event.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
        owner.commit()
        with operation_lock(owner, f"recall:{recall_id}") as acquired:
            assert acquired
            owner.commit()
            with Session(recall_database) as contender:
                result = await RecallWorkerService(
                    client, contender, settings=make_settings()
                ).poll()
                assert result.completed == result.failed == 0
                with pytest.raises(RecallResponseError, match="already"):
                    await RecallResponseService(client, contender).respond(recall_id)
                client.move_product.assert_not_awaited()
                client.notify_recall.assert_not_awaited()
        with Session(recall_database) as restarted:
            assert (
                await RecallWorkerService(
                    client, restarted, settings=make_settings()
                ).poll()
            ).completed == 1
        assert client.move_product.await_count == client.notify_recall.await_count == 1


@pytest.mark.anyio
async def test_lost_messaging_response_creates_one_official_oc_and_recovers_without_llm(
    recall_database,
):
    deadline = datetime.now(UTC) + timedelta(hours=2)

    class Interpreter:
        calls = 0

        async def interpret(self, event):
            self.calls += 1
            event.decision = MessagingDecision.ORDER
            event.interpreted_sku = "KIT-ANALGESIA"
            event.interpreted_quantity = 2
            event.interpreted_delivery_date = deadline
            event.llm_output = {"simulation": True}

    class RemoteMessaging:
        created = 0
        requests = 0

        async def respond_order(self, *args, **kwargs):
            self.requests += 1
            if self.created:
                raise MessagingAlreadyRespondedError(409)
            self.created += 1
            raise MessagingTimeoutError("Response lost after official OC creation")

    remote, interpreter, official = RemoteMessaging(), Interpreter(), AsyncMock()
    official.get_order.return_value = make_purchase_order(
        sku="KIT-ANALGESIA",
        cantidad=2,
        vencimiento=deadline,
        nombreCliente="Farmacia",
    )
    with Session(recall_database) as session:
        event = add_event(session, received_at=datetime.now(UTC))
        worker = MessagingProcessingService(
            remote, interpreter, official, session, settings=make_settings()
        )
        assert (await worker.process_pending()).failed == 1
        event.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        assert (await worker.process_pending()).failed == 1
        assert event.next_retry_at is None
        assert remote.created == interpreter.calls == 1
        assert remote.requests == 2
        assert (await worker.process_pending()).processed == 0
    with Session(recall_database) as restarted:
        recovery = MessagingRecoveryService(
            official, restarted, settings=make_settings()
        )
        order = await recovery.recover("msg-worker", EXTERNAL_ORDER_ID)
        assert (await recovery.recover("msg-worker", EXTERNAL_ORDER_ID)).id == order.id
        assert remote.created == interpreter.calls == 1
        assert restarted.scalar(select(func.count()).select_from(Order)) == 1
        assert official.update_order_state.await_count == 1


@pytest.mark.anyio
async def test_notification_timeout_is_durable_and_does_not_schedule_blind_retry(
    recall_database,
):
    with Session(recall_database) as session:
        _, _, client, _, _ = recall_setup(session)
        client.notify_recall.side_effect = FarmaCentralTimeoutError("Unknown receipt")
        worker = RecallWorkerService(client, session, settings=make_settings())
        assert (await worker.poll()).failed == 1
        notification = session.scalars(select(RecallNotification)).one()
        assert notification.status == RecallNotificationStatus.FAILED
        assert notification.response["delivery_state"] == "uncertain"
        assert session.scalars(select(InboundEvent)).one().next_retry_at is None
        assert (await worker.poll()).failed == 0
        assert client.notify_recall.await_count == 1


@pytest.mark.anyio
async def test_stale_resolution_cannot_undo_another_sessions_confirmation(
    recall_database,
):
    with (
        Session(recall_database) as owner,
        Session(recall_database, expire_on_commit=False) as stale,
    ):
        _, recall_id, client, _, order = recall_setup(owner)
        client.notify_recall.side_effect = FarmaCentralTimeoutError("Receipt lost")
        await RecallWorkerService(client, owner, settings=make_settings()).poll()
        notification = owner.scalars(select(RecallNotification)).one()
        cached = stale.get(RecallNotification, notification.id)
        assert cached.status == RecallNotificationStatus.FAILED
        resolve_notification(
            owner,
            recall_id,
            order.external_order_id,
            outcome="sent",
            evidence="Official receipt",
            sent_at=datetime.now(UTC),
        )
        with pytest.raises(NotificationResolutionError, match="overwrite"):
            resolve_notification(
                stale,
                recall_id,
                order.external_order_id,
                outcome="not-sent",
                evidence="Stale evidence",
            )
        owner.expire_all()
        assert notification.status == RecallNotificationStatus.SENT


@pytest.mark.anyio
@pytest.mark.parametrize("accepted_remotely", [False, True])
async def test_uncertain_notification_needs_evidence_and_never_replays_blindly(
    recall_database, accepted_remotely
):
    receipts = []
    with Session(recall_database) as session:
        _, recall_id, client, _, order = recall_setup(session)
        order_id = order.external_order_id

        async def send(*args):
            if accepted_remotely:
                receipts.append(datetime.now(UTC))
            raise ProcessStopped

        client.notify_recall.side_effect = send
        with pytest.raises(ProcessStopped):
            await RecallWorkerService(client, session, settings=make_settings()).poll()

    with Session(recall_database) as restarted:
        event = restarted.scalars(select(InboundEvent)).one()
        event.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
        restarted.commit()
        worker = RecallWorkerService(client, restarted, settings=make_settings())
        assert (await worker.poll()).failed == 1
        notification = restarted.scalars(select(RecallNotification)).one()
        assert notification.status == RecallNotificationStatus.PENDING
        assert event.next_retry_at is None
        assert client.notify_recall.await_count == 1
        assert (await worker.poll()).completed == 0
        with pytest.raises(RecallNotificationUncertainError):
            await RecallResponseService(client, restarted).respond(recall_id)
        assert client.notify_recall.await_count == 1

        resolution = resolve_notification(
            restarted,
            recall_id,
            order_id,
            outcome="sent" if accepted_remotely else "not-sent",
            evidence="Simulated official receipt"
            if accepted_remotely
            else "Simulated request never arrived",
            sent_at=receipts[0] if accepted_remotely else None,
        )
        assert resolution["requeued"]

        async def confirmed(*args):
            receipts.append(datetime.now(UTC))

        client.notify_recall.side_effect = confirmed
        assert (await worker.poll()).completed == 1
        assert len(receipts) == 1
        assert client.notify_recall.await_count == (1 if accepted_remotely else 2)
        assert client.move_product.await_count == 1
        assert notification.response["resolutions"][0]["evidence"].startswith(
            "Simulated"
        )
        assert restarted.scalar(select(func.count()).select_from(CustodyEvent)) == 1
        assert (await worker.poll()).completed == 0
        if accepted_remotely:
            assert not resolve_notification(
                restarted,
                recall_id,
                order_id,
                outcome="sent",
                evidence="Same official receipt",
                sent_at=receipts[0],
            )["requeued"]
            with pytest.raises(NotificationResolutionError, match="overwrite"):
                resolve_notification(
                    restarted,
                    recall_id,
                    order_id,
                    outcome="not-sent",
                    evidence="Contradictory claim",
                )


@pytest.mark.anyio
async def test_move_accepted_before_timeout_is_reconciled_without_second_move(
    recall_database,
):
    with Session(recall_database) as session:
        _, recall_id, client, unit, _ = recall_setup(session, delivery=False)
        client.move_product.side_effect = FarmaCentralTimeoutError(
            "Response lost after move"
        )
        worker = RecallWorkerService(client, session, settings=make_settings())
        assert (await worker.poll()).failed == 1
        event = session.scalars(select(InboundEvent)).one()
        event.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        client.get_space_products.return_value = [{"_id": unit.external_unit_id}]
        assert (await worker.poll()).completed == 1
        assert client.move_product.await_count == 1
        assert unit.status == "quarantined"
        assert session.scalar(select(func.count()).select_from(CustodyEvent)) == 1


@pytest.mark.anyio
async def test_report_can_be_replaced_after_crash_without_repeating_other_actions(
    recall_database,
):
    reports = []
    with Session(recall_database) as session:
        _, _, client, _, _ = recall_setup(session)

        async def report(recall_id, payload):
            reports.append(payload.model_dump(by_alias=True))
            if len(reports) == 1:
                raise ProcessStopped

        client.report_recall.side_effect = report
        with pytest.raises(ProcessStopped):
            await RecallWorkerService(client, session, settings=make_settings()).poll()
    with Session(recall_database) as restarted:
        event = restarted.scalars(select(InboundEvent)).one()
        event.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
        restarted.commit()
        assert (
            await RecallWorkerService(
                client, restarted, settings=make_settings()
            ).poll()
        ).completed == 1
        assert reports[0] == reports[1]
        assert client.move_product.await_count == client.notify_recall.await_count == 1


@pytest.mark.anyio
async def test_explicit_notification_rate_limit_is_retryable_and_honors_backoff(
    recall_database,
):
    with Session(recall_database) as session:
        _, _, client, _, _ = recall_setup(session, stock=False)
        client.notify_recall.side_effect = FarmaCentralHTTPError(
            429, retry_after_seconds=120
        )
        now = datetime.now(UTC)
        assert (
            await RecallWorkerService(client, session, settings=make_settings()).poll()
        ).failed == 1
        event = session.scalars(select(InboundEvent)).one()
        notification = session.scalars(select(RecallNotification)).one()
        assert notification.response["delivery_state"] == "rejected"
        assert event.next_retry_at >= now + timedelta(seconds=120)
        assert (
            await RecallWorkerService(client, session, settings=make_settings()).poll()
        ).completed == 0
        assert client.notify_recall.await_count == 1


def test_resolution_requires_evidence_and_cannot_fabricate_a_future_timestamp(
    db_session,
):
    with pytest.raises(NotificationResolutionError):
        resolve_notification(
            db_session,
            "rcl",
            EXTERNAL_ORDER_ID,
            outcome="sent",
            evidence="",
            sent_at=datetime.now(UTC),
        )
    with pytest.raises(NotificationResolutionError, match="future"):
        resolve_notification(
            db_session,
            "rcl",
            EXTERNAL_ORDER_ID,
            outcome="sent",
            evidence="receipt",
            sent_at=datetime.now(UTC) + timedelta(hours=1),
        )
