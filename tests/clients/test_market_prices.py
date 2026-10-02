import httpx
import pytest

from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
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
