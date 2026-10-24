from types import SimpleNamespace

import pytest

from app.services.orders import PubSubPollResult
from scripts import poll_pubsub_orders as module


@pytest.mark.anyio
async def test_once_creates_subscription_polls_and_closes_resources(monkeypatch):
    state = SimpleNamespace(subscription_created=False, polled=0, closed=[])

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

        async def ensure_subscription(self):
            state.subscription_created = True

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def poll(self):
            state.polled += 1
            return PubSubPollResult(received=1, processed=1)

    settings = SimpleNamespace(
        pubsub_poll_interval_seconds=5,
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "PubSubOrdersClient", Context)
    monkeypatch.setattr(module, "PurchaseOrdersClient", Context)
    monkeypatch.setattr(module, "SessionLocal", Context)
    monkeypatch.setattr(module, "PubSubOrderIngestionService", Service)

    await module.run(once=True)

    assert state.subscription_created
    assert state.polled == 1
    assert len(state.closed) == 2
