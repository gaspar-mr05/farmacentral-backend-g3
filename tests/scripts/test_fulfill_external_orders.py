from types import SimpleNamespace

import pytest

from app.services.external_order_fulfillment import ExternalOrderFulfillmentResult
from scripts import fulfill_external_orders as module


@pytest.mark.anyio
async def test_once_processes_orders_and_closes_clients(monkeypatch):
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
            return ExternalOrderFulfillmentResult(completed=1)

    settings = SimpleNamespace(external_order_poll_interval_seconds=5)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "FarmaCentralClient", Context)
    monkeypatch.setattr(module, "PurchaseOrdersClient", Context)
    monkeypatch.setattr(module, "SessionLocal", Context)
    monkeypatch.setattr(module, "ExternalOrderFulfillmentService", Service)

    await module.run(once=True)

    assert state.processed == 1
    assert len(state.closed) == 2
