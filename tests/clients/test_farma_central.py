import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import FarmaCentralHTTPError
from tests.support.farma_central import make_settings


@pytest.mark.anyio
async def test_read_operations_use_expected_endpoints_and_reuse_token() -> None:
    authentication_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal authentication_requests
        if request.url.path == "/api/auth":
            authentication_requests += 1
            return httpx.Response(200, json={"token": "test-token"})

        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path == "/api/spaces/store-1/products":
            assert request.url.params["sku"] == "SKU-1"
            assert request.url.params["limit"] == "200"
        return httpx.Response(200, json={"path": request.url.path})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)

        available_response = await client.get_available_products()
        spaces_response = await client.get_spaces()
        inventory_response = await client.get_space_inventory("store-1")
        products_response = await client.get_space_products(
            "store-1",
            "SKU-1",
            limit=200,
        )

    assert available_response == {"path": "/api/products/available"}
    assert spaces_response == {"path": "/api/spaces"}
    assert inventory_response == {"path": "/api/spaces/store-1/inventory"}
    assert products_response == {"path": "/api/spaces/store-1/products"}
    assert authentication_requests == 1


@pytest.mark.anyio
async def test_get_refreshes_rejected_token_once() -> None:
    issued_tokens = iter(("expired-token", "fresh-token"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            return httpx.Response(200, json={"token": next(issued_tokens)})
        if request.headers["Authorization"] == "Bearer expired-token":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)

        response = await client.get("/products")

    assert response == {"status": "ok"}


@pytest.mark.anyio
async def test_get_retries_rate_limit_response() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/api/auth":
            return httpx.Response(200, json={"token": "test-token"})
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)
        with patch("app.clients.farma_central.asyncio.sleep", new=AsyncMock()) as sleep:
            response = await client.get("/products")

    assert response == {"status": "ok"}
    assert attempts == 2
    sleep.assert_awaited_once_with(1.0)


@pytest.mark.anyio
async def test_http_error_preserves_safe_provider_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(400, json={"detail": "Invalid fabrication nonce"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)

        with pytest.raises(
            FarmaCentralHTTPError,
            match="HTTP 400: Invalid fabrication nonce",
        ):
            await client.get("/products")


@pytest.mark.anyio
async def test_move_product_patches_destination_store_and_accepts_empty_response() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            return httpx.Response(200, json={"token": "test-token"})

        assert request.method == "PATCH"
        assert request.url.path == "/api/products/unit-1"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert json.loads(request.content) == {"store": "store-2"}
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)

        result = await client.move_product("unit-1", "store-2")

    assert result is None


@pytest.mark.anyio
async def test_supply_operations_use_expected_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            return httpx.Response(200, json={"token": "test-token"})
        if request.url.path == "/api/fabrication/challenge":
            assert request.method == "POST"
            assert json.loads(request.content) == {"sku": "SUPPLY-1", "quantity": 10}
            return httpx.Response(201, json={"challengeId": "challenge-1"})

        assert request.method == "POST"
        assert request.url.path == "/api/products"
        assert json.loads(request.content) == {
            "sku": "SUPPLY-1",
            "quantity": 10,
            "challengeId": "challenge-1",
            "nonce": "42",
        }
        return httpx.Response(201, json={"availableAt": "2099-01-01T00:00:00Z"})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)

        challenge = await client.request_fabrication_challenge("SUPPLY-1", 10)
        result = await client.request_products(
            sku="SUPPLY-1",
            quantity=10,
            challenge_id="challenge-1",
            nonce="42",
        )

    assert challenge == {"challengeId": "challenge-1"}
    assert result == {"availableAt": "2099-01-01T00:00:00Z"}
