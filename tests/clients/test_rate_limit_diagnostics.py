import logging

import httpx
import pytest

from app.clients import rate_limit_diagnostics as module
from app.clients.farma_central import FarmaCentralClient
from app.clients.market_prices import MarketPriceClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.trades import TradesClient
from tests.support.farma_central import make_settings


def test_counts_include_auth_and_are_sampled_without_exposing_secrets(
    monkeypatch, caplog
):
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    caplog.set_level(logging.INFO, logger=module.__name__)
    diagnostics = module.RateLimitDiagnostics("test")

    def response(status=200):
        return httpx.Response(
            status,
            request=httpx.Request(
                "POST",
                "https://example.test/autenticar?secret=PRIVATE_QUERY",
                headers={"Authorization": "PRIVATE_AUTH"},
            ),
            headers={
                "RateLimit": "limit=250, remaining=0, reset=10",
                "Retry-After": "10",
                "Set-Cookie": "PRIVATE_COOKIE",
            },
            json={"token": "PRIVATE_TOKEN"},
        )

    diagnostics.observe(response())
    caplog.clear()
    now[0] = 1
    diagnostics.observe(response())
    diagnostics.observe(response())
    assert not caplog.records
    now[0] = 30
    diagnostics.observe(response())
    assert len(caplog.records) == 1
    assert "responses=3 authentication=3 limited=0 elapsed_seconds=30.0" in caplog.text
    caplog.clear()
    now[0] = 31
    diagnostics.observe(response(429))
    assert "responses=1 authentication=1 limited=1 elapsed_seconds=1.0" in caplog.text
    assert "limit=250, remaining=0, reset=10" in caplog.text
    assert "PRIVATE" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "client_type,service",
    [
        (FarmaCentralClient, "farma_central"),
        (PurchaseOrdersClient, "purchase_orders"),
        (TradesClient, "trades"),
        (MarketPriceClient, "market_prices"),
    ],
)
async def test_clients_observe_existing_responses_without_extra_requests(
    client_type, service, caplog
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[], headers={"RateLimit-Remaining": "249"})

    caplog.set_level(logging.INFO, logger=module.__name__)
    async with httpx.AsyncClient(
        base_url="https://example.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = client_type(settings=make_settings(), http_client=http_client)
        if client_type is MarketPriceClient:
            response = await client._request_prices()
        elif client_type is PurchaseOrdersClient:
            response = await client._send_unpaced("GET", "orders/example")
        else:
            response = await client._send("GET", "example")

    assert response.status_code == 200
    assert len(requests) == 1
    assert f"service={service} host=example.test responses=1" in caplog.text
    assert "'ratelimit-remaining': '249'" in caplog.text
