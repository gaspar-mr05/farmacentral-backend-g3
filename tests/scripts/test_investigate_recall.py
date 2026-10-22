import json

import pytest
from sqlalchemy.orm import Session

from scripts import investigate_recall as module
from tests.services.test_recalls import recall_database as recall_database
from tests.support.recalls import RecallInventory


def test_cli_outputs_persisted_local_investigation(
    recall_database, monkeypatch, capsys
):
    with Session(recall_database) as session:
        inventory = RecallInventory(session)
        root = inventory.lot("root")
        inventory.unit("stock", root)
        recall = inventory.recall(root)
        recall_id = recall.recall_id
        session.commit()
    monkeypatch.setattr(module, "SessionLocal", lambda: Session(recall_database))
    monkeypatch.setattr("sys.argv", ["investigate_recall", recall_id])

    module.main()

    result = json.loads(capsys.readouterr().out)
    assert result["recall_id"] == recall_id
    assert len(result["lots"]) == len(result["stock_units"]) == 1
    assert result["orders"] == []


def test_cli_reports_missing_recall_with_nonzero_exit(
    recall_database, monkeypatch, caplog
):
    monkeypatch.setattr(module, "SessionLocal", lambda: Session(recall_database))
    monkeypatch.setattr("sys.argv", ["investigate_recall", "unknown"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 1
    assert "Recall unknown was not found" in caplog.text
