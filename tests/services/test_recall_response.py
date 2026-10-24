from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralTimeoutError
from app.db.units import upsert_units
from app.models import (
    CustodyEvent,
    Recall,
    RecallNotification,
    RecallNotificationStatus,
    RecallStatus,
    RecallUnit,
    Unit,
)
from app.queries.catalog import list_sellable_catalog
from app.schemas.units import UnitData
from app.services.recalls import RecallResponseError, RecallResponseService
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


@pytest.mark.anyio
async def test_blocks_all_stock_before_external_calls_and_keeps_block_on_failure(
    recall_database,
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        lot = inventory.lot("root")
        stock = [inventory.unit(str(number), lot) for number in range(3)]
        recall = inventory.recall(lot)
        session.commit()
        client = AsyncMock()

        async def unavailable_spaces():
            with Session(recall_database) as observer:
                assert set(observer.scalars(select(Unit.status))) == {"recall_pending"}
                assert (
                    sum(
                        item.stock
                        for item in list_sellable_catalog(
                            observer, as_of=datetime.now(UTC)
                        )
                    )
                    == 0
                )
            raise FarmaCentralTimeoutError("Temporary failure")

        client.get_spaces.side_effect = unavailable_spaces
        with pytest.raises(FarmaCentralTimeoutError):
            await RecallResponseService(client, session).respond(recall.recall_id)

        assert all(unit.status == "recall_pending" for unit in stock)
        assert recall.status is RecallStatus.FAILED
        client.move_product.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("completed", [False, True])
async def test_sync_cannot_restore_recalled_stock_from_an_old_snapshot(
    recall_database, completed
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        lot = inventory.lot("root")
        unit = inventory.unit("stock", lot)
        recall = inventory.recall(lot)
        session.commit()
        old_record = UnitData(
            external_unit_id=unit.external_unit_id,
            lot_external_id=lot.external_lot_id,
            location_code=inventory.locations["storage"].code,
            status="available",
            effective_expires_at=unit.effective_expires_at,
        )
        client = AsyncMock()
        client.get_spaces.return_value = [
            {"_id": inventory.locations["quarantine"].code, "quarantine": True}
        ]
        if completed:
            await RecallResponseService(client, session).respond(recall.recall_id)
        else:
            client.get_spaces.side_effect = FarmaCentralTimeoutError("Temporary")
            with pytest.raises(FarmaCentralTimeoutError):
                await RecallResponseService(client, session).respond(recall.recall_id)

        upsert_units(
            session,
            [old_record],
            {lot.external_lot_id: lot},
            {location.code: location for location in inventory.locations.values()},
        )
        session.commit()

        assert unit.status == ("quarantined" if completed else "recall_pending")
        expected = "quarantine" if completed else "storage"
        assert unit.current_location_id == inventory.locations[expected].id
        assert (
            sum(
                item.stock
                for item in list_sellable_catalog(session, as_of=datetime.now(UTC))
            )
            == 0
        )


@pytest.mark.anyio
async def test_delivery_completed_during_response_is_not_quarantined(recall_database):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        lot = inventory.lot("root")
        unit = inventory.unit("stock", lot)
        recall = inventory.recall(lot)
        unit_id = unit.id
        session.commit()
        client = AsyncMock()

        async def spaces_after_dispatch():
            # An operation already in flight confirms a delivery after the snapshot.
            with Session(recall_database) as dispatcher:
                other = RecallInventory(dispatcher)
                other.order((dispatcher.get(Unit, unit_id), True))
                dispatcher.commit()
            return [{"_id": inventory.locations["quarantine"].code, "quarantine": True}]

        client.get_spaces.side_effect = spaces_after_dispatch
        result = await RecallResponseService(client, session).respond(recall.recall_id)

        client.move_product.assert_not_awaited()
        assert result.notified_orders == 1
        assert session.get(Unit, unit_id).status == "dispatched"
        report = client.report_recall.await_args.args[1]
        assert report.in_stock == []
        assert report.deliveries[0].product_ids == [unit.external_unit_id]


@pytest.mark.anyio
async def test_expired_recall_does_not_send_external_actions(recall_database):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        lot = inventory.lot("root")
        inventory.unit("stock", lot)
        recall = inventory.recall(lot)
        recall.issued_at = datetime.now(UTC) - timedelta(hours=1)
        recall.deadline = datetime.now(UTC) - timedelta(minutes=30)
        session.commit()
        client = AsyncMock()

        with pytest.raises(RecallResponseError, match="deadline"):
            await RecallResponseService(client, session).respond(recall.recall_id)

        client.get_spaces.assert_not_awaited()
        client.report_recall.assert_not_awaited()


@pytest.mark.anyio
async def test_stale_sync_session_cannot_undo_quarantine_and_blocks_new_affected_units(
    recall_database,
):
    with Session(recall_database) as session, Session(recall_database) as stale_sync:
        inventory = RecallInventory(session)
        lot = inventory.lot("root")
        unrelated = inventory.lot("unrelated")
        unit = inventory.unit("stock", lot)
        recall = inventory.recall(lot)
        session.commit()
        stale_unit = stale_sync.get(Unit, unit.id)
        assert stale_unit.status == "available"
        client = AsyncMock()
        client.get_spaces.return_value = [
            {"_id": inventory.locations["quarantine"].code, "quarantine": True}
        ]
        await RecallResponseService(client, session).respond(recall.recall_id)
        records = [
            UnitData(
                external_unit_id=external_id,
                lot_external_id=source_lot.external_lot_id,
                location_code=inventory.locations["storage"].code,
                status="available",
                effective_expires_at=inventory.expiry,
            )
            for external_id, source_lot in [
                (unit.external_unit_id, lot),
                ("new-affected", lot),
                ("new-unrelated", unrelated),
            ]
        ]

        upsert_units(
            stale_sync,
            records,
            {lot.external_lot_id: lot, unrelated.external_lot_id: unrelated},
            {location.code: location for location in inventory.locations.values()},
        )
        stale_sync.commit()

        assert stale_unit.status == "quarantined"
        assert stale_unit.current_location_id == inventory.locations["quarantine"].id
        statuses = dict(
            stale_sync.execute(select(Unit.external_unit_id, Unit.status)).all()
        )
        assert statuses["new-affected"] == "recall_pending"
        assert statuses["new-unrelated"] == "available"
