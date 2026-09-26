import httpx
import pytest

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import (
    FarmaCentralAuthenticationError,
    FarmaCentralInvalidResponseError,
    FarmaCentralTimeoutError,
)
from tests.farma_central_support import make_settings


@pytest.mark.anyio
async def test_authentication_rejection_raises_specific_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "invalid credentials"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(FarmaCentralAuthenticationError) as error:
            await client.get("/products")

    assert error.value.status_code == 400


@pytest.mark.anyio
async def test_timeout_is_translated_to_integration_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(FarmaCentralTimeoutError):
            await client.get("/products")


@pytest.mark.anyio
async def test_authentication_requires_token_in_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "value"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(FarmaCentralInvalidResponseError):
            await client.get("/products")
