import json

import httpx
import pytest

from app.clients.farma_central import FarmaCentralClient
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
        return httpx.Response(200, json={"path": request.url.path})

    async with httpx.AsyncClient(
        base_url="https://example.test/api/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = FarmaCentralClient(settings=make_settings(), http_client=http_client)

        available_response = await client.get_available_products()
        spaces_response = await client.get_spaces()
        inventory_response = await client.get_space_inventory("store-1")
        products_response = await client.get_space_products("store-1", "SKU-1")

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
