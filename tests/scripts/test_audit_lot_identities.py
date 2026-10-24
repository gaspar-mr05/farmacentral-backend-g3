import json
import sys

import pytest
from sqlalchemy.orm import Session

from scripts import audit_lot_identities as module
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory


@pytest.mark.parametrize("filter_official_sku", [False, True])
def test_audit_reports_unresolved_history_without_changing_it(
    recall_database, monkeypatch, capsys, filter_official_sku
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        provisional = inventory.lot("pending", "RAW")
        provisional.external_lot_id = "unreported:missing-batch"
        unit = inventory.unit("missing-batch", provisional, status="consumed")
        official = inventory.lot("L-OFFICIAL")
        inventory.unit("official", official)
        official_sku = official.product.sku
        unit_id = unit.id
        session.commit()
    monkeypatch.setattr(module, "SessionLocal", lambda: Session(recall_database))
    args = ["audit_lot_identities"]
    if filter_official_sku:
        args.extend(["--sku", official_sku])
    monkeypatch.setattr(sys, "argv", args)

    if filter_official_sku:
        module.main()
    else:
        with pytest.raises(SystemExit) as exc:
            module.main()
        assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["unresolved_units"] == (0 if filter_official_sku else 1)
    with Session(recall_database) as session:
        from app.models import Unit

        assert session.get(Unit, unit_id).status == "consumed"
        assert (
            session.get(Unit, unit_id).lot.external_lot_id == "unreported:missing-batch"
        )
