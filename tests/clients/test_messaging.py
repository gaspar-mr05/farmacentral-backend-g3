import json
from datetime import UTC, datetime

import httpx
import pytest

from app.clients.messaging import MessagingClient
from app.clients.messaging_exceptions import (
    MessagingAlreadyRespondedError,
    MessagingAuthenticationError,
    MessagingConnectionError,
    MessagingHTTPError,
    MessagingInvalidResponseError,
    MessagingTimeoutError,
)
from tests.support.farma_central import make_settings

MESSAGE_ID = "msg_01JB7Q2F4R8S"
ORDER_ID = "64abc123def456789012abcd"
DEADLINE = datetime(2026, 10, 27, 22, tzinfo=UTC)


@pytest.mark.anyio
async def test_register_order_uses_documented_contract_and_reuses_token():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/messaging/auth":
            assert json.loads(request.content) == {"group": 3, "secret": "test-secret"}
            return httpx.Response(200, json={"token": "test-token"})
        assert request.url.path == f"/messaging/messages/{MESSAGE_ID}/response"
        assert request.headers["Authorization"] == "Bearer test-token"
        assert json.loads(request.content) == {
            "status": "order",
            "interpretation": {
                "sku": "KIT-RESP-ADULTO",
                "quantity": 2,
                "deliveryDate": "2026-10-27T22:00:00Z",
            },
        }
        return httpx.Response(200, json={"orderId": ORDER_ID.upper()})

    async with httpx.AsyncClient(
        base_url="https://messaging.test/messaging/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        first = await client.respond_order(
            MESSAGE_ID,
            sku="KIT-RESP-ADULTO",
            quantity=2,
            delivery_date=DEADLINE,
        )
        second = await client.respond_order(
            MESSAGE_ID,
            sku="KIT-RESP-ADULTO",
            quantity=2,
            delivery_date=DEADLINE,
        )

    assert first == second == ORDER_ID
    assert len(requests) == 3


@pytest.mark.anyio
async def test_discard_uses_reason_and_escapes_message_id():
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        assert request.url.raw_path == b"/messages/msg%2Funsafe/response"
        assert json.loads(request.content) == {
            "status": "discarded",
            "reason": "No corresponde a un pedido",
        }
        return httpx.Response(200, json={"status": "discarded"})

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        await client.discard("msg/unsafe", reason="No corresponde a un pedido")


@pytest.mark.anyio
async def test_unauthorized_response_refreshes_token_once():
    auth_count = 0
    response_count = 0

    def handler(request):
        nonlocal auth_count, response_count
        if request.url.path.endswith("/auth"):
            auth_count += 1
            return httpx.Response(200, json={"token": f"token-{auth_count}"})
        response_count += 1
        if response_count == 1:
            return httpx.Response(401)
        assert request.headers["Authorization"] == "Bearer token-2"
        return httpx.Response(200, json={"status": "discarded"})

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        await client.discard(MESSAGE_ID, reason="No es un pedido")

    assert (auth_count, response_count) == (2, 2)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, MessagingHTTPError),
        (401, MessagingAuthenticationError),
        (409, MessagingAlreadyRespondedError),
        (410, MessagingHTTPError),
        (422, MessagingHTTPError),
        (500, MessagingHTTPError),
    ],
)
async def test_http_failures_are_typed_without_logging_secrets(status, error, caplog):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(status, json={"message": "test-secret test-token"})

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(error):
            await client.discard(MESSAGE_ID, reason="No es un pedido")

    assert "test-secret" not in caplog.text
    assert "test-token" not in caplog.text


@pytest.mark.anyio
async def test_rate_limit_exposes_retry_after():
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(429, headers={"Retry-After": "17"})

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(MessagingHTTPError) as error:
            await client.discard(MESSAGE_ID, reason="No es un pedido")

    assert error.value.retry_after_seconds == 17


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("transport_error", "expected"),
    [
        (httpx.ReadTimeout, MessagingTimeoutError),
        (httpx.ConnectError, MessagingConnectionError),
    ],
)
async def test_transport_failures_are_reported(transport_error, expected):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        raise transport_error("simulated failure", request=request)

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(expected):
            await client.discard(MESSAGE_ID, reason="No es un pedido")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("order", {}),
        ("order", {"orderId": "invalid"}),
        ("discard", {}),
        ("discard", {"orderId": ORDER_ID, "status": "discarded"}),
    ],
)
async def test_invalid_success_responses_are_rejected(operation, payload):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(MessagingInvalidResponseError):
            if operation == "order":
                await client.respond_order(
                    MESSAGE_ID,
                    sku="KIT-RESP-ADULTO",
                    quantity=2,
                    delivery_date=DEADLINE,
                )
            else:
                await client.discard(MESSAGE_ID, reason="No es un pedido")
