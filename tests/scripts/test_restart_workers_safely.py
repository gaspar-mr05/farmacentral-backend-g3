from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from scripts import restart_workers_safely as script


@pytest.mark.parametrize("period", ["period-id", None])
def test_restart_waits_and_keeps_lock_held_until_systemd_finishes(monkeypatch, period):
    monkeypatch.setattr(
        script, "get_settings", lambda: SimpleNamespace(billing_period_id=period)
    )
    session = MagicMock()
    monkeypatch.setattr(script, "SessionLocal", Mock(return_value=session))
    attempts = 0
    held = False

    @contextmanager
    def lock(*args):
        nonlocal attempts, held
        attempts += 1
        if attempts < 3:
            raise script.ProductionBusyError()
        held = True
        try:
            yield
        finally:
            held = False

    monkeypatch.setattr(script, "replenishment_lock", lock)
    monkeypatch.setattr(script, "production_lock", lock)
    monkeypatch.setattr(script, "sleep", lambda _: None)

    def restart(command, *, check):
        assert held and check
        assert command == [
            "sudo",
            "-n",
            "/usr/bin/systemctl",
            "restart",
            "farmacentral-workers.target",
        ]

    called = Mock(side_effect=restart)
    monkeypatch.setattr(script.subprocess, "run", called)
    script.run()
    assert attempts == 3
    called.assert_called_once()
    assert not held


def test_busy_timeout_leaves_workers_running(monkeypatch):
    monkeypatch.setattr(
        script, "get_settings", lambda: SimpleNamespace(billing_period_id="period")
    )
    monkeypatch.setattr(script, "SessionLocal", MagicMock())
    monkeypatch.setattr(
        script, "replenishment_lock", Mock(side_effect=script.ProductionBusyError())
    )
    called = Mock()
    monkeypatch.setattr(script.subprocess, "run", called)
    with pytest.raises(RuntimeError, match="were not restarted"):
        script.run(timeout_seconds=0)
    called.assert_not_called()
