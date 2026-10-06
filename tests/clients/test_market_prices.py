import asyncio

import httpx
import pytest

from app.clients.farma_central_exceptions import (
    FarmaCentralHTTPError,
    FarmaCentralInvalidResponseError,
)
from app.clients.market_prices import MarketPriceClient
from tests.support.farma_central import make_settings


@pytest.mark.anyio
async def test_current_prices_use_public_endpoint_without_authentication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/market/prices"
        assert "Authorization" not in request.headers
        return httpx.Response(
            200,
            json=[
                {
                    "sku": "KIT-1",
                    "price": 1500,
                    "fairValue": 1400,
                    "updatedAt": "2026-10-02T12:00:00Z",
                }
            ],
        )

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MarketPriceClient(settings=make_settings(), http_client=http_client)

        result = await client.get_current_prices()

    assert result[0]["sku"] == "KIT-1"


@pytest.mark.anyio
async def test_current_prices_reject_non_list_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sku": "KIT-1"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MarketPriceClient(settings=make_settings(), http_client=http_client)

        with pytest.raises(
            FarmaCentralInvalidResponseError,
            match="unexpected JSON value",
        ):
            await client.get_current_prices()


@pytest.mark.anyio
async def test_current_prices_are_cached_for_the_configured_ttl() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MarketPriceClient(
            settings=make_settings(market_price_cache_ttl_seconds=30),
            http_client=http_client,
        )

        first = await client.get_current_prices()
        second = await client.get_current_prices()

    assert first == second == []
    assert requests == 1


@pytest.mark.anyio
async def test_current_prices_can_bypass_cache_for_order_pricing() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MarketPriceClient(
            settings=make_settings(market_price_cache_ttl_seconds=30),
            http_client=http_client,
        )

        await client.get_current_prices()
        await client.get_current_prices(use_cache=False)

    assert requests == 2


@pytest.mark.anyio
async def test_concurrent_price_requests_share_one_refresh() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MarketPriceClient(
            settings=make_settings(market_price_cache_ttl_seconds=30),
            http_client=http_client,
        )

        results = await asyncio.gather(
            client.get_current_prices(),
            client.get_current_prices(),
            client.get_current_prices(),
        )

    assert results == [[], [], []]
    assert requests == 1


@pytest.mark.anyio
async def test_rate_limit_is_retried_when_wait_is_short() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MarketPriceClient(
            settings=make_settings(market_price_max_retry_wait_seconds=2),
            http_client=http_client,
        )

        result = await client.get_current_prices()

    assert result == []
    assert requests == 2


@pytest.mark.anyio
async def test_rate_limit_exposes_retry_delay_when_wait_is_too_long() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"RateLimit": "limit=250, reset=60"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MarketPriceClient(
            settings=make_settings(market_price_max_retry_wait_seconds=2),
            http_client=http_client,
        )

        with pytest.raises(FarmaCentralHTTPError) as error:
            await client.get_current_prices()

    assert error.value.status_code == 429
    assert error.value.retry_after_seconds == 60
