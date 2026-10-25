"""Run automated tests with external connections blocked and isolated DB schemas."""

import argparse
import os
import socket
import sys
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy.engine import make_url

from app.core.config import get_settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
STEP20_TESTS = [
    "tests/services/test_e2_idempotency.py",
    "tests/services/test_sftp_orders.py",
    "tests/services/test_pubsub_orders.py",
    "tests/services/test_messaging_processing.py",
    "tests/services/test_messaging_recovery.py",
    "tests/services/test_recall_response.py",
    "tests/services/test_recall_worker.py",
    "tests/services/test_order_processing.py",
    "tests/services/test_external_order_regressions.py",
    "tests/api/test_messaging.py",
    "tests/api/test_recalls.py",
    "tests/api/test_order_dispatch.py",
    "tests/models/test_inbound_event.py",
    "tests/models/test_e2_orders.py",
    "tests/models/test_recalls.py",
    "tests/scripts/test_resolve_recall_notification.py",
]
STEP21_TESTS = [
    "tests/api/test_health.py",
    "tests/db/test_health.py",
    "tests/api/test_traceability.py",
    "tests/services/traceability",
    "tests/services/test_continuous_operation.py",
    "tests/scripts/test_check_operation.py",
    "tests/services/test_e2_idempotency.py",
    "tests/services/test_recall_investigation_performance.py",
]


@contextmanager
def local_connections_only():
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guard(method):
        def guarded(sock, address):
            if isinstance(address, tuple) and address[0] not in LOCAL_HOSTS:
                raise AssertionError("External connections disabled during tests")
            return method(sock, address)

        return guarded

    with (
        patch.object(socket.socket, "connect", guard(connect)),
        patch.object(socket.socket, "connect_ex", guard(connect_ex)),
    ):
        yield


def main(argv=()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--step20",
        action="store_true",
        help="Pruebas de duplicados, concurrencia y recuperación",
    )
    scope.add_argument(
        "--step21",
        action="store_true",
        help="Salud, aislamiento de API, rendimiento y reinicios de procesos locales",
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get("TEST_DATABASE_URL") or get_settings().database_url
    if make_url(database_url).host not in LOCAL_HOSTS:
        raise SystemExit(
            "Configura TEST_DATABASE_URL con PostgreSQL local para estas pruebas"
        )
    try:
        import pytest
    except ImportError:
        raise SystemExit(
            "Instala las dependencias de desarrollo: pip install -e '.[dev]'"
        ) from None
    with (
        patch.dict(os.environ, {"TEST_DATABASE_URL": database_url}),
        local_connections_only(),
    ):
        paths = (
            STEP20_TESTS if args.step20 else STEP21_TESTS if args.step21 else ["tests"]
        )
        return int(pytest.main(["-q", *paths]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
