from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models import (
    CustodyEvent,
    Recall,
    RecallNotification,
    RecallNotificationStatus,
    RecallStatus,
    RecallUnit,
)
from app.services.recalls import RecallResponseService
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory


@pytest.mark.anyio
async def test_responds_with_quarantine_notifications_and_report(recall_database):
    from sqlalchemy.orm import Session

    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        root = inventory.lot("root", "RAW")
        derived = inventory.lot("derived")
        consumed = inventory.unit("consumed", root)
        inventory.link(derived, root, consumed_unit=consumed)
        stock = inventory.unit("stock", derived, location="storage")
        delivered = inventory.unit("delivered", derived)
        order = inventory.order((delivered, True))
        recall = inventory.recall(root)
        session.commit()

        client = AsyncMock()
        client.get_spaces.return_value = [
            {
                "_id": inventory.locations["quarantine"].code,
                "quarantine": True,
            }
        ]

        result = await RecallResponseService(client, session).respond(recall.recall_id)

        assert result.quarantined_units == 1
        assert result.notified_orders == 1
        assert stock.status == "quarantined"
        assert stock.current_location_id == inventory.locations["quarantine"].id
        assert session.scalar(select(RecallUnit)).quarantined_at is not None
        assert session.scalar(select(func.count()).select_from(CustodyEvent)) == 1

        saved = session.get(Recall, recall.id)
        assert saved.status is RecallStatus.COMPLETED
        assert saved.reported_at is not None
        notification = session.scalar(select(RecallNotification))
        assert notification.status is RecallNotificationStatus.SENT

        client.move_product.assert_awaited_once_with(
            stock.external_unit_id, inventory.locations["quarantine"].code
        )
        notification_request = client.notify_recall.await_args.args[1]
        assert notification_request.order_id == order.external_order_id
        assert notification_request.product_ids == [delivered.external_unit_id]

        report = client.report_recall.await_args.args[1]
        assert report.in_stock[0].product_id == stock.external_unit_id
        assert report.in_stock[0].store == inventory.locations["storage"].code
        assert report.derived_batches[0].batch == derived.external_lot_id
        assert report.derived_batches[0].source_batch == root.external_lot_id
        assert report.deliveries[0].product_ids == [delivered.external_unit_id]


@pytest.mark.anyio
async def test_retry_does_not_move_or_notify_twice(recall_database):
    from sqlalchemy.orm import Session

    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        root = inventory.lot("root")
        inventory.unit("stock", root)
        delivered = inventory.unit("delivered", root)
        inventory.order((delivered, True))
        recall = inventory.recall(root)
        session.commit()

        client = AsyncMock()
        client.get_spaces.return_value = [
            {
                "_id": inventory.locations["quarantine"].code,
                "quarantine": True,
            }
        ]
        service = RecallResponseService(client, session)

        await service.respond(recall.recall_id)
        await service.respond(recall.recall_id)

        client.move_product.assert_awaited_once()
        client.notify_recall.assert_awaited_once()
        assert client.report_recall.await_count == 2
