from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    InboundEvent,
    InboundEventStatus,
    LotOrigin,
    OrderUnit,
    ProductionRun,
    Recall,
    RecallStatus,
    Unit,
)
from app.schemas.inventory import InventoryData
from app.schemas.locations import LocationData
from app.schemas.lots import LotData
from app.schemas.units import UnitData
from app.services.inventory.sync import sync_inventory
from app.services.recalls import RecallWorkerService
from tests.services.test_recalls import recall_database as recall_database
from tests.support.farma_central import make_settings
from tests.support.recalls import RecallInventory


@pytest.mark.anyio
async def test_late_delivery_updates_same_customer_notification(recall_database):
    with Session(recall_database, expire_on_commit=False) as session:
        inventory = RecallInventory(session)
        origin = inventory.lot("origin", "RAW")
        first = inventory.unit("delivered", origin)
        order = inventory.order((first, True))
        client = AsyncMock()
        client.get_active_recalls.return_value = [
            {
                "recallId": "rcl-deliveries",
                "batch": origin.external_lot_id,
                "sku": origin.product.sku,
                "reason": "Calidad",
                "issuedAt": inventory.now,
                "deadline": inventory.expiry,
            }
        ]
        client.get_spaces.return_value = [
            {
                "_id": inventory.locations["quarantine"].code,
                "quarantine": True,
            }
        ]
        session.commit()
        worker = RecallWorkerService(client, session, settings=make_settings())
        assert (await worker.poll()).completed == 1
        client.notify_recall.assert_awaited_once()

        second = inventory.unit("late-delivery", origin, status="dispatched")
        order.items[0].quantity = 2
        order.items[0].assigned_units.append(
            OrderUnit(unit=second, dispatched_at=inventory.now)
        )
        session.commit()
        assert (await worker.poll()).completed == 1
        assert client.notify_recall.await_count == 2
        assert set(client.notify_recall.call_args.args[1].product_ids) == {
            first.external_unit_id,
            second.external_unit_id,
        }
        assert (await worker.poll()).completed == 0
        assert client.notify_recall.await_count == 2
        assert client.report_recall.await_count == 2


@pytest.mark.anyio
@pytest.mark.parametrize("expired", [False, True])
async def test_late_production_is_blocked_and_report_updated_only_before_deadline(
    recall_database, expired
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        origin = inventory.lot("origin", "RAW")
        raw = inventory.unit("raw", origin, status="consumed")
        output = inventory.lot("output")
        inventory.link(None, origin, consumed_unit=raw)
        run = session.scalars(select(ProductionRun)).one()
        run.expected_sku = output.product.sku
        session.commit()
        client = AsyncMock()
        client.get_active_recalls.return_value = [
            {
                "recallId": "rcl-late",
                "batch": origin.external_lot_id,
                "sku": origin.product.sku,
                "reason": "Calidad",
                "issuedAt": inventory.now,
                "deadline": inventory.expiry,
            }
        ]
        client.get_spaces.return_value = [
            {
                "_id": inventory.locations["quarantine"].code,
                "quarantine": True,
            }
        ]
        worker = RecallWorkerService(client, session, settings=make_settings())
        assert (await worker.poll()).completed == 1
        recall = session.scalars(select(Recall)).one()
        if expired:
            recall.issued_at = inventory.now - timedelta(minutes=2)
            recall.deadline = inventory.now - timedelta(seconds=1)
            recall.reported_at = inventory.now - timedelta(seconds=2)
            session.commit()

        location = inventory.locations["buffer"]
        sync_inventory(
            session,
            InventoryData(
                products=(),
                locations=(LocationData(location.code, "Buffer", False),),
                lots=(
                    LotData(
                        output.external_lot_id,
                        output.product.sku,
                        inventory.expiry,
                        LotOrigin.OWN_PRODUCTION,
                    ),
                ),
                units=(
                    UnitData(
                        "late-output",
                        output.external_lot_id,
                        location.code,
                        "available",
                        inventory.expiry,
                    ),
                ),
            ),
        )
        session.commit()
        unit = session.scalar(
            select(Unit).where(Unit.external_unit_id == "late-output")
        )
        assert run.output_lot_id == output.id
        assert unit.status == "recall_pending"

        second = await worker.poll()
        assert second.completed == (0 if expired else 1)
        assert unit.status == ("recall_pending" if expired else "quarantined")
        assert client.report_recall.await_count == (1 if expired else 2)
        if not expired:
            report = client.report_recall.call_args.args[1]
            assert {item.product_id for item in report.in_stock} == {"late-output"}
            assert {item.batch for item in report.derived_batches} == {
                output.external_lot_id
            }
            assert (await worker.poll()).completed == 0
            assert client.report_recall.await_count == 2


@pytest.mark.anyio
async def test_poll_receives_and_completes_active_recall(recall_database):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        origin = inventory.lot("origin", "RAW")
        unit = inventory.unit("stock", origin)
        session.commit()

        client = AsyncMock()
        client.get_active_recalls.return_value = [
            {
                "recallId": "rcl-worker",
                "batch": origin.external_lot_id,
                "sku": origin.product.sku,
                "reason": "Calidad",
                "issuedAt": inventory.now,
                "deadline": inventory.expiry,
            }
        ]
        client.get_spaces.return_value = [
            {
                "_id": inventory.locations["quarantine"].code,
                "quarantine": True,
            }
        ]

        result = await RecallWorkerService(
            client,
            session,
            settings=make_settings(recall_batch_size=5),
        ).poll()

        assert result.received == 1
        assert result.completed == 1
        assert result.failed == 0
        assert session.scalars(select(InboundEvent)).one().status is (
            InboundEventStatus.PROCESSED
        )
        assert session.scalars(select(Recall)).one().status is RecallStatus.COMPLETED
        client.move_product.assert_awaited_once_with(
            unit.external_unit_id, inventory.locations["quarantine"].code
        )
        client.report_recall.assert_awaited_once()


@pytest.mark.anyio
async def test_poll_retries_recall_when_origin_lot_is_not_available(recall_database):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        client = AsyncMock()
        client.get_active_recalls.return_value = [
            {
                "recallId": "rcl-missing-lot",
                "batch": "missing-batch",
                "sku": "RAW-MISSING",
                "reason": "Calidad",
                "issuedAt": inventory.now,
                "deadline": inventory.expiry,
            }
        ]

        result = await RecallWorkerService(
            client,
            session,
            settings=make_settings(
                recall_batch_size=5,
                recall_poll_interval_seconds=30,
            ),
        ).poll()

        event = session.scalars(select(InboundEvent)).one()
        assert result.failed == 1
        assert event.status is InboundEventStatus.FAILED
        assert event.next_retry_at is not None
        assert "not synchronized" in event.last_error


@pytest.mark.anyio
async def test_completed_recall_is_not_processed_twice(recall_database):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        origin = inventory.lot("origin", "RAW")
        inventory.unit("stock", origin)
        session.commit()

        client = AsyncMock()
        client.get_active_recalls.return_value = [
            {
                "recallId": "rcl-once",
                "batch": origin.external_lot_id,
                "sku": origin.product.sku,
                "reason": "Calidad",
                "issuedAt": inventory.now,
                "deadline": inventory.expiry,
            }
        ]
        client.get_spaces.return_value = [
            {
                "_id": inventory.locations["quarantine"].code,
                "quarantine": True,
            }
        ]
        worker = RecallWorkerService(
            client,
            session,
            settings=make_settings(recall_batch_size=5),
        )

        first = await worker.poll()
        second = await worker.poll()

        assert first.completed == 1
        assert second.received == 0
        assert second.completed == 0
        client.report_recall.assert_awaited_once()


@pytest.mark.anyio
async def test_invalid_active_recall_does_not_block_valid_recalls(
    recall_database, caplog
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        origin = inventory.lot("valid-origin")
        inventory.unit("stock", origin)
        session.commit()
        valid = {
            "recallId": "valid-recall",
            "batch": origin.external_lot_id,
            "sku": origin.product.sku,
            "reason": "Calidad",
            "issuedAt": inventory.now,
            "deadline": inventory.expiry,
        }
        client = AsyncMock()
        client.get_active_recalls.return_value = [
            {**valid, "recallId": "bad", "batch": None, "sku": None, "reason": None},
            valid,
            "untrusted-private-payload",
        ]
        client.get_spaces.return_value = [
            {"_id": inventory.locations["quarantine"].code, "quarantine": True}
        ]

        result = await RecallWorkerService(
            client, session, settings=make_settings()
        ).poll()

        assert result.invalid == 2
        assert result.received == result.completed == 1
        assert result.failed == 0
        assert not result.connection_failed
        assert (
            session.scalars(select(InboundEvent)).one().external_event_id
            == "valid-recall"
        )
        assert "untrusted-private-payload" not in caplog.text
        client.report_recall.assert_awaited_once()


@pytest.mark.anyio
async def test_bad_active_list_still_allows_persisted_webhook_to_complete(
    recall_database,
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        origin = inventory.lot("webhook-origin")
        inventory.unit("stock", origin)
        from app.schemas.recalls import RecallWebhookPayload
        from app.services.recalls import RecallWebhookService

        RecallWebhookService(session).receive(
            RecallWebhookPayload(
                recallId="webhook-recall",
                batch=origin.external_lot_id,
                sku=origin.product.sku,
                reason="Calidad",
                issuedAt=inventory.now,
                deadline=inventory.expiry,
            )
        )
        client = AsyncMock()
        client.get_active_recalls.return_value = [{"batch": None}]
        client.get_spaces.return_value = [
            {"_id": inventory.locations["quarantine"].code, "quarantine": True}
        ]

        result = await RecallWorkerService(
            client, session, settings=make_settings()
        ).poll()

        assert result.invalid == 1
        assert result.completed == 1
        assert result.failed == 0
