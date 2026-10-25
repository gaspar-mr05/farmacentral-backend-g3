import socket
from unittest.mock import Mock, patch

import pytest

from scripts import test_e2 as module


def test_network_guard_blocks_external_connect_and_connect_ex():
    original = socket.socket.connect
    connect = Mock()
    connect_ex = Mock()
    with (
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", connect_ex),
        module.local_connections_only(),
    ):
        for method in (socket.socket.connect, socket.socket.connect_ex):
            with pytest.raises(AssertionError, match="External connections"):
                method(object(), ("203.0.113.1", 443))
            method(object(), ("127.0.0.1", 5432))
        assert connect.call_count == connect_ex.call_count == 1
    assert socket.socket.connect is original


def test_runner_rejects_nonlocal_database_without_printing_credentials(monkeypatch):
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+psycopg://user:private-key@remote.test/db"
    )
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert "PostgreSQL local" in str(exc.value)
    assert "private-key" not in str(exc.value)


def test_runner_invokes_tests_with_external_connections_blocked(monkeypatch):
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/test"
    )

    def main(args):
        assert args == ["-q", "tests"]
        with pytest.raises(AssertionError):
            socket.socket.connect(object(), ("203.0.113.1", 443))
        return 0

    monkeypatch.setattr(pytest, "main", main)
    assert module.main() == 0


def test_step20_runner_selects_idempotency_cases_without_external_connections(
    monkeypatch,
):
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/test"
    )
    runner = Mock(return_value=0)
    monkeypatch.setattr(pytest, "main", runner)
    assert module.main(["--step20"]) == 0
    assert runner.call_args.args[0] == ["-q", *module.STEP20_TESTS]


def test_step21_runner_selects_operation_cases_without_external_connections(
    monkeypatch,
):
    monkeypatch.setenv(
        "TEST_DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/test"
    )
    runner = Mock(return_value=0)
    monkeypatch.setattr(pytest, "main", runner)
    assert module.main(["--step21"]) == 0
    assert runner.call_args.args[0] == ["-q", *module.STEP21_TESTS]
