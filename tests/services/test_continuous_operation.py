import os
import select as io_select
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import InboundEvent, InboundEventStatus
from tests.services.test_recalls import recall_database as recall_database
from tests.support.traceability import create_traceability_scenario


def process_env(engine):
    with engine.connect() as connection:
        schema = connection.scalar(text("SHOW search_path"))
    url = engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    return {
        **os.environ,
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "FARMA_CENTRAL_BASE_URL": "https://example.test/",
        "FARMA_CENTRAL_API_SECRET": "unused-test-secret",
    }


@contextmanager
def child(engine, mode, *args, pass_fds=()):
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.support.operation_process", mode, *args],
        env=process_env(engine),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=pass_fds,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def wait_api(process, client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert process.poll() is None, "Local API exited before startup"
        try:
            if client.get("/api/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.02)
    raise AssertionError("Local API did not start within 10 seconds")


def test_real_process_restart_keeps_pending_events_and_portal_queries(recall_database):
    with Session(recall_database) as session:
        scenario = create_traceability_scenario(session)
        session.commit()

    with child(recall_database, "worker-stop") as worker:
        assert io_select.select([worker.stdout], [], [], 10)[0]
        assert worker.stdout.readline().strip() == "persisted"

        # The web process stays available while a separate worker holds its lock.
        for _ in range(2):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                with (
                    child(
                        recall_database,
                        "api",
                        str(listener.fileno()),
                        pass_fds=(listener.fileno(),),
                    ) as api,
                    httpx.Client(
                        base_url=f"http://127.0.0.1:{port}", timeout=2
                    ) as client,
                ):
                    wait_api(api, client)
                    assert client.get("/api/health/ready").status_code == 200
                    response = client.get(f"/api/traceability/{scenario.kit_lot_id}")
                    assert response.status_code == 200
                    assert len(response.json()["ancestors"]) == 2
                    assert worker.poll() is None
            with Session(recall_database) as session:
                assert (
                    session.scalars(select(InboundEvent)).one().status
                    == InboundEventStatus.PROCESSING
                )

        worker.kill()
        worker.wait(timeout=10)

    with child(recall_database, "worker-recover") as worker:
        stdout, _ = worker.communicate(timeout=10)
        assert worker.returncode == 0
        assert stdout.strip() == "recovered"

    with Session(recall_database) as session:
        event = session.scalars(select(InboundEvent)).one()
        assert event.status == InboundEventStatus.PROCESSED
        assert event.responded_at is not None
        assert event.attempt_count == 2
