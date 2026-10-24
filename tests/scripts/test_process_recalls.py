from types import SimpleNamespace

import pytest

from app.services.recalls import RecallWorkerResult
from scripts import process_recalls as module


@pytest.mark.anyio
async def test_once_processes_recalls_and_closes_client(monkeypatch):
    state = SimpleNamespace(processed=0, closed=False)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed = True

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        async def poll(self):
            state.processed += 1
            return RecallWorkerResult(completed=1)

    settings = SimpleNamespace(recall_poll_interval_seconds=30)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "FarmaCentralClient", Client)
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(module, "RecallWorkerService", Worker)

    await module.run(once=True)

    assert state.processed == 1
    assert state.closed is True


@pytest.mark.anyio
async def test_once_exits_with_error_when_recall_processing_fails(monkeypatch):
    class Context:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        async def poll(self):
            return RecallWorkerResult(failed=1)

    settings = SimpleNamespace(recall_poll_interval_seconds=30)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "FarmaCentralClient", Context)
    monkeypatch.setattr(module, "SessionLocal", Context)
    monkeypatch.setattr(module, "RecallWorkerService", Worker)

    with pytest.raises(SystemExit) as error:
        await module.run(once=True)

    assert error.value.code == 1
