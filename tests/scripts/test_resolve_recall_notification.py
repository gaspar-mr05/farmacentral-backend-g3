import json
import sys
from unittest.mock import MagicMock, Mock

import pytest

from app.services.recalls.notification_recovery import NotificationResolutionError
from scripts import resolve_recall_notification as module


def test_resolution_cli_records_provided_evidence_without_clients(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resolve_recall_notification",
            "rcl-test",
            "a" * 24,
            "--outcome",
            "sent",
            "--evidence",
            "Official receipt reference",
            "--sent-at",
            "2026-10-24T20:00:00Z",
        ],
    )
    sessions = MagicMock()
    resolver = Mock(return_value={"requeued": True})
    monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(module, "resolve_notification", resolver)
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == {"requeued": True}
    assert resolver.call_args.kwargs["evidence"] == "Official receipt reference"
    assert resolver.call_args.kwargs["sent_at"].utcoffset() is not None


@pytest.mark.parametrize(
    "extra",
    [
        ["--outcome", "sent", "--evidence", "receipt"],
        ["--outcome", "not-sent", "--evidence", " "],
        [
            "--outcome",
            "sent",
            "--evidence",
            "receipt",
            "--sent-at",
            "2026-10-24T20:00:00",
        ],
    ],
)
def test_invalid_resolution_never_opens_database(monkeypatch, extra):
    monkeypatch.setattr(
        sys, "argv", ["resolve_recall_notification", "rcl", "a" * 24, *extra]
    )
    sessions = Mock()
    monkeypatch.setattr(module, "SessionLocal", sessions)
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    sessions.assert_not_called()


def test_resolution_error_is_reported_without_retrying(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resolve_recall_notification",
            "rcl",
            "a" * 24,
            "--outcome",
            "not-sent",
            "--evidence",
            "Official failure record",
        ],
    )
    monkeypatch.setattr(module, "SessionLocal", MagicMock())
    resolver = Mock(
        side_effect=NotificationResolutionError("No notification attempt exists")
    )
    monkeypatch.setattr(module, "resolve_notification", resolver)
    assert module.main() == 1
    assert "No notification attempt" in json.loads(capsys.readouterr().out)["error"]
    resolver.assert_called_once()
