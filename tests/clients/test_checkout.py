import httpx
import pytest

from app.clients.checkout import CheckoutClient
from tests.support.farma_central import make_settings


@pytest.mark.anyio
async def test_checkout_uses_documented_auth_and_init_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/payments/auth":
            return httpx.Response(200, json={"success": True, "token": "jwt-token"})
        return httpx.Response(
            201,
            json={
                "payment_id": "payment-123",
                "redirect_url": "https://checkout.test/payment-123",
            },
        )

    async with httpx.AsyncClient(
        base_url="https://checkout.test/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = CheckoutClient(settings=make_settings(), http_client=http_client)
        result = await client.initialize_payment(
            amount=9990,
            back_urls={
                "success": "https://shop.test/success",
                "error": "https://shop.test/error",
                "cancelled": "https://shop.test/cancelled",
            },
        )

    assert result["payment_id"] == "payment-123"
    assert requests[0].url.path == "/payments/auth"
    assert requests[1].url.path == "/payments/init"
    assert requests[1].headers["Authorization"] == "Bearer jwt-token"
