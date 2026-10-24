import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralInvalidResponseError
from app.models import Lot, LotIdentityRepair, ProductionInput, Unit
from app.schemas.units import UnitData
from app.services.inventory.lot_identity import recover_reported_lots
from app.services.inventory.sync import InventorySyncService
from app.services.recalls import RecallInvestigationService
from tests.services.inventory.test_sync import FakeInventorySource
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory


def evidence(unit, batch):
    return UnitData(
        external_unit_id=unit.external_unit_id,
        lot_external_id=batch,
        location_code=unit.current_location.code,
        status="available",
        effective_expires_at=unit.effective_expires_at,
    )


def provisional(inventory, name, sku="RAW"):
    lot = inventory.lot(name, sku)
    unit = inventory.unit(name, lot)
    lot.external_lot_id = f"unreported:{unit.external_unit_id}"
    inventory.session.flush()
    return lot, unit


def test_official_evidence_recovers_inputs_deliveries_and_recall_lineage(
    recall_database,
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        first, consumed = provisional(inventory, "first")
        second, stock = provisional(inventory, "second")
        unrelated, untouched = provisional(inventory, "unrelated")
        output = inventory.lot("derived")
        inventory.link(output, first, consumed_unit=consumed)
        consumed.status = "consumed"
        delivered = inventory.unit("delivered", output)
        inventory.order((delivered, True))
        original_names = {
            consumed.id: first.external_lot_id,
            stock.id: second.external_lot_id,
        }
        official = "L-OFFICIAL-RAW"
        records = (evidence(consumed, official), evidence(stock, official))
        sku = first.product.sku
        session.commit()

        assert recover_reported_lots(session, records, {official: sku}) == 2
        session.commit()
        target = session.scalar(select(Lot).where(Lot.external_lot_id == official))
        assert consumed.lot_id == stock.lot_id == target.id
        assert session.scalar(select(ProductionInput)).input_lot_id == target.id
        assert untouched.lot_id == unrelated.id
        assert consumed.status == "consumed"
        audit = session.scalars(select(LotIdentityRepair)).all()
        assert {row.unit_id: row.previous_batch for row in audit} == original_names
        assert recover_reported_lots(session, records, {official: sku}) == 0
        assert session.scalar(select(func.count()).select_from(LotIdentityRepair)) == 2

        recall = inventory.recall(target)
        session.commit()
        findings = RecallInvestigationService(session).investigate(recall.recall_id)
        assert {lot.external_lot_id for lot in findings.lots} == {
            official,
            output.external_lot_id,
        }
        assert [unit.unit_id for unit in findings.stock_units] == [stock.id]
        assert findings.orders[0].deliveries[0].unit_id == delivered.id


@pytest.mark.parametrize("existing_target", [False, True])
def test_recovery_preserves_a_production_output(recall_database, existing_target):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        raw = inventory.lot("input", "RAW")
        output = inventory.lot("output")
        inventory.link(output, raw)
        run = output.produced_by
        session.refresh(output)
        run = output.produced_by
        output.external_lot_id = f"production:{run.id}"
        units = [inventory.unit(str(i), output) for i in range(2)]
        batch = "L-OFFICIAL-OUTPUT"
        target = inventory.lot(batch) if existing_target else None
        if target is not None:
            target.external_lot_id = batch
        records = tuple(evidence(unit, batch) for unit in units)
        sku = output.product.sku
        session.commit()

        assert recover_reported_lots(session, records, {batch: sku}) == 2
        session.commit()

        assert run.output_lot.external_lot_id == batch
        assert {unit.lot_id for unit in units} == {run.output_lot_id}
        assert run.inputs[0].input_lot_id == raw.id


@pytest.mark.parametrize("problem", ["partial", "conflicting_batches", "wrong_sku"])
def test_recovery_rejects_incomplete_or_conflicting_evidence(recall_database, problem):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        lot = inventory.lot("output")
        lot.external_lot_id = "production:provisional"
        units = [inventory.unit(str(i), lot) for i in range(2)]
        batch = "L-OFFICIAL"
        records = tuple(evidence(unit, batch) for unit in units)
        skus = {batch: lot.product.sku}
        if problem == "partial":
            records = records[:1]
        elif problem == "conflicting_batches":
            records = (records[0], evidence(units[1], "OTHER"))
            skus["OTHER"] = lot.product.sku
        else:
            skus[batch] = "WRONG"
        session.commit()

        with pytest.raises(FarmaCentralInvalidResponseError):
            recover_reported_lots(session, records, skus)
        session.rollback()

        assert lot.external_lot_id == "production:provisional"
        assert session.scalar(select(func.count()).select_from(LotIdentityRepair)) == 0


def test_missing_evidence_does_not_guess_from_sku_or_expiry(recall_database):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        lot, unit = provisional(inventory, "unknown")
        previous = lot.external_lot_id
        session.commit()

        assert recover_reported_lots(session, (evidence(unit, previous),), {}) == 0
        assert lot.external_lot_id == previous


@pytest.mark.anyio
async def test_sync_recovers_the_official_batch_without_losing_consumption_history(
    recall_database,
):
    with Session(recall_database) as session:
        client = FakeInventorySource()
        client.include_batch = False
        service = InventorySyncService(client, session)
        await service.synchronize()
        unit = session.scalar(
            select(Unit).where(Unit.external_unit_id == client.unit_id)
        )
        source_id = unit.lot_id
        inventory = RecallInventory(session)
        output = inventory.lot("derived")
        inventory.link(output, unit.lot, consumed_unit=unit)
        unit.status = "consumed"
        session.commit()
        client.include_batch = True

        await service.synchronize()

        assert unit.lot_id == source_id
        assert unit.lot.external_lot_id == client.lot_id
        assert unit.status == "consumed"
        assert session.scalar(select(ProductionInput)).input_lot_id == source_id
        assert session.scalar(select(func.count()).select_from(LotIdentityRepair)) == 1
        client.include_batch = False
        await service.synchronize()
        assert unit.lot.external_lot_id == client.lot_id


def test_recovery_does_not_rewrite_existing_recall_snapshots(recall_database):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        source, unit = provisional(inventory, "audited")
        recall = inventory.recall(source)
        session.commit()
        RecallInvestigationService(session).investigate(recall.recall_id)
        target = inventory.lot("target", "RAW")
        target.external_lot_id = "L-EXISTING"
        session.commit()

        with pytest.raises(FarmaCentralInvalidResponseError, match="snapshot"):
            recover_reported_lots(
                session,
                (evidence(unit, target.external_lot_id),),
                {target.external_lot_id: target.product.sku},
            )
        session.rollback()

        assert unit.lot_id == source.id
        assert source.external_lot_id.startswith("unreported:")
