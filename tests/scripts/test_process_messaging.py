from types import SimpleNamespace

import pytest

from app.services.messaging_processing import MessagingProcessingResult
from scripts import process_messaging as module


@pytest.mark.anyio
async def test_once_processes_messages_and_closes_clients(monkeypatch):
    state = SimpleNamespace(processed=0, closed=[])

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
            state.closed.append(self)

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def process_pending(self):
            state.processed += 1
            return MessagingProcessingResult(processed=1)

    settings = SimpleNamespace(messaging_poll_interval_seconds=12)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "MessagingClient", Context)
    monkeypatch.setattr(module, "LLMGatewayClient", Context)
    monkeypatch.setattr(module, "PurchaseOrdersClient", Context)
    monkeypatch.setattr(module, "SessionLocal", Context)
    monkeypatch.setattr(module, "MessageInterpretationService", Context)
    monkeypatch.setattr(module, "MessagingProcessingService", Service)

    await module.run(once=True)

    assert state.processed == 1
    assert len(state.closed) == 3


@pytest.mark.anyio
async def test_once_exits_with_error_when_an_event_fails(monkeypatch):
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

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def process_pending(self):
            return MessagingProcessingResult(failed=1)

    settings = SimpleNamespace(messaging_poll_interval_seconds=12)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "MessagingClient", Context)
    monkeypatch.setattr(module, "LLMGatewayClient", Context)
    monkeypatch.setattr(module, "PurchaseOrdersClient", Context)
    monkeypatch.setattr(module, "SessionLocal", Context)
    monkeypatch.setattr(module, "MessageInterpretationService", Context)
    monkeypatch.setattr(module, "MessagingProcessingService", Service)

    with pytest.raises(SystemExit) as error:
        await module.run(once=True)

    assert error.value.code == 1
