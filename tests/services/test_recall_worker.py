from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import InboundEvent, InboundEventStatus, Recall, RecallStatus
from app.services.recalls import RecallWorkerService
from tests.services.test_recalls import recall_database as recall_database
from tests.support.farma_central import make_settings
from tests.support.recalls import RecallInventory


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
