import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import test_e3 as module


def test_metrics_count_incorrect_predictions_and_keep_splits_separate():
    summary = module.summarize_corpus(
        [
            {"split": "development", "category": "order", "checks": {"intent": True}},
            {"split": "development", "category": "order", "checks": {"intent": False}},
            {
                "split": "evaluation",
                "category": "inquiry",
                "checks": {"selected_order": False},
            },
        ]
    )
    assert summary["development"]["order"]["intent"] == {
        "correct": 1,
        "total": 2,
        "accuracy": 0.5,
    }
    assert summary["evaluation"]["inquiry"]["selected_order"]["accuracy"] == 0


def test_runner_replaces_both_database_urls_and_restores_environment(
    monkeypatch, tmp_path
):
    original = "postgresql+psycopg://private:secret@operational.test/db"
    isolated = "postgresql+psycopg://e3_test@127.0.0.1:12345/postgres"
    monkeypatch.setenv("DATABASE_URL", original)
    monkeypatch.setenv("TEST_DATABASE_URL", original)

    @contextmanager
    def database():
        yield isolated

    monkeypatch.setattr(module, "isolated_postgres", database)

    def run(args, plugins):
        assert os.environ["DATABASE_URL"] == os.environ["TEST_DATABASE_URL"] == isolated
        assert args == ["-q", "tests/services/test_messaging_corpus.py"]
        import socket

        with pytest.raises(AssertionError, match="External connections"):
            socket.socket.connect(object(), ("203.0.113.1", 443))
        return 1

    monkeypatch.setattr(pytest, "main", run)
    report = tmp_path / "report.json"
    assert module.main(["--corpus-only", "--report", str(report)]) == 1
    assert original not in report.read_text()
    assert os.environ["DATABASE_URL"] == os.environ["TEST_DATABASE_URL"] == original


def test_startup_failure_never_falls_back_to_configured_database(monkeypatch, capsys):
    @contextmanager
    def fail():
        raise RuntimeError("private database details")
        yield

    monkeypatch.setattr(module, "isolated_postgres", fail)
    run = Mock()
    monkeypatch.setattr(pytest, "main", run)
    assert module.main([]) == 2
    run.assert_not_called()
    assert "private database" not in capsys.readouterr().err


def test_cluster_is_stopped_and_deleted_when_test_execution_fails(monkeypatch):
    monkeypatch.setattr(module, "postgres_bin", lambda name: name)
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)
    with (
        pytest.raises(RuntimeError, match="interrupted"),
        module.isolated_postgres() as url,
    ):
        assert "127.0.0.1" in url
        raise RuntimeError("interrupted")
    commands = [call.args[0] for call in run.call_args_list]
    assert commands[-1][-1] == "stop"
    root = Path(commands[0][commands[0].index("-D") + 1]).parent
    assert not root.exists()


def test_isolated_postgres_uses_docker_when_native_tools_are_missing(monkeypatch):
    used = []

    @contextmanager
    def docker_database():
        used.append(True)
        yield "postgresql+psycopg://e3_test@127.0.0.1:54321/postgres"

    monkeypatch.setattr(module, "native_postgres_tools_available", lambda *args: False)
    monkeypatch.setattr(module, "docker_postgres", docker_database)

    with module.isolated_postgres() as url:
        assert url.endswith(":54321/postgres")

    assert used == [True]
