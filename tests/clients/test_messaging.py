import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from app.clients.messaging import (
    MessagingAlreadyRespondedError,
    MessagingAuthenticationError,
    MessagingClient,
    MessagingConnectionError,
    MessagingHTTPError,
    MessagingInvalidResponseError,
    MessagingTimeoutError,
)
from app.schemas.messaging import MessagingWebhookPayload
from tests.support.farma_central import make_settings

MESSAGE_ID = "msg_01JB7Q2F4R8S"
ORDER_ID = "64abc123def456789012abcd"
DEADLINE = datetime(2026, 10, 27, 22, tzinfo=UTC)


def incoming_message():
    return MessagingWebhookPayload.model_validate(
        {
            "messageId": MESSAGE_ID,
            "sender": {"name": "Farmacia de prueba", "phone": "+56 9 5555 0123"},
            "text": "¿Cómo va mi pedido?",
            "receivedAt": "2026-11-12T20:00:00Z",
        }
    )


async def new_operation(client, operation):
    if operation == "inquiry":
        return await client.respond_inquiry(MESSAGE_ID, order_id=ORDER_ID)
    return await client.send_reply(incoming_message(), text="Su OC está aceptada.")


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

    assert first.order_id == second.order_id == ORDER_ID
    assert first.test is False
    assert len(requests) == 3


@pytest.mark.anyio
async def test_inquiry_and_reply_use_separate_endpoints_and_original_routing():
    requests = []
    reply = {"accepted": True, "messageId": "opaque-outbound-id"}

    def handler(request):
        requests.append(request)
        if request.url.path == "/messaging/auth":
            return httpx.Response(200, json={"token": "test-token"})
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/response"):
            assert request.url.path == f"/messaging/messages/{MESSAGE_ID}/response"
            assert json.loads(request.content) == {
                "status": "inquiry",
                "orderId": ORDER_ID,
            }
            return httpx.Response(200, json={"status": "inquiry", "orderId": ORDER_ID})
        assert request.url.path == "/messaging/messages"
        assert json.loads(request.content) == {
            "to": incoming_message().sender.phone,
            "replyTo": MESSAGE_ID,
            "text": "Su OC está aceptada.",
        }
        return httpx.Response(201, json=reply)

    async with httpx.AsyncClient(
        base_url="https://messaging.test/messaging/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        classification = await client.respond_inquiry(
            MESSAGE_ID, order_id=ORDER_ID.upper()
        )
        sent = await client.send_reply(incoming_message(), text="Su OC está aceptada.")

    assert classification == {"status": "inquiry", "orderId": ORDER_ID}
    assert sent == reply
    assert len(requests) == 3


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["inquiry", "send"])
@pytest.mark.parametrize("status", [200, 201, 204])
async def test_new_operations_accept_http_confirmations_without_inventing_ids(
    operation,
    status,
):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return (
            httpx.Response(status) if status == 204 else httpx.Response(status, json={})
        )

    async with httpx.AsyncClient(
        base_url="https://messaging.test/messaging/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        assert await new_operation(client, operation) == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"status": "discarded"},
        {"status": "order"},
        {"orderId": "74abc123def456789012abcd"},
        {"orderId": None},
        {"orderId": 123},
    ],
)
async def test_inquiry_does_not_accept_a_conflicting_classification_or_order(payload):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(MessagingInvalidResponseError):
            await client.respond_inquiry(MESSAGE_ID, order_id=ORDER_ID)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["inquiry", "send"])
@pytest.mark.parametrize("payload", [[], None, "not an object", {"success": False}])
async def test_new_operations_reject_invalid_confirmation_bodies(operation, payload):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, content=json.dumps(payload))

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(MessagingInvalidResponseError):
            await new_operation(client, operation)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["inquiry", "send"])
async def test_new_operations_reject_non_json_success(operation):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, text="<html>error</html>")

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(MessagingInvalidResponseError):
            await new_operation(client, operation)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["inquiry", "send"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422, 429, 500, 503])
async def test_new_operation_errors_preserve_status_without_blind_retries(
    operation,
    status,
    caplog,
):
    posts = []

    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        posts.append(request)
        return httpx.Response(
            status,
            json={"message": "test-secret test-token"},
            headers={"Retry-After": "30"},
        )

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(MessagingHTTPError) as error:
            await new_operation(client, operation)
    assert error.value.status_code == status
    assert error.value.retry_after_seconds == (30 if status == 429 else None)
    if status == 409:
        assert isinstance(error.value, MessagingAlreadyRespondedError) == (
            operation == "inquiry"
        )
    assert len(posts) == (2 if status == 401 else 1)
    assert "test-secret" not in caplog.text + str(error.value)
    assert "test-token" not in caplog.text + str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["inquiry", "send"])
async def test_uncertain_send_or_classification_is_not_automatically_repeated(
    operation,
):
    posts = []

    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        posts.append(request)
        raise httpx.ReadTimeout(
            "Response lost after applying the request", request=request
        )

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(MessagingTimeoutError):
            await new_operation(client, operation)
    assert len(posts) == 1


@pytest.mark.anyio
async def test_invalid_inquiry_id_and_blank_reply_are_rejected_before_http():
    def handler(request):
        pytest.fail("Invalid input must not send any request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(ValidationError):
            await client.respond_inquiry(MESSAGE_ID, order_id="../another-order")
        with pytest.raises(ValueError):
            await client.respond_inquiry(" ", order_id=ORDER_ID)
        with pytest.raises(ValidationError):
            await client.send_reply(incoming_message(), text=" ")


@pytest.mark.anyio
@pytest.mark.parametrize("order_id", [None, ORDER_ID])
async def test_test_marker_preserves_optional_order_id(order_id):
    def handler(request):
        if request.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, json={"test": True, "orderId": order_id})

    async with httpx.AsyncClient(
        base_url="https://messaging.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = MessagingClient(settings=make_settings(), http_client=http_client)
        response = await client.respond_order(
            MESSAGE_ID, sku="KIT-RESP-ADULTO", quantity=2, delivery_date=DEADLINE
        )
    assert response.test is True
    assert response.order_id == order_id


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
        ("order", {"test": False}),
        ("order", {"test": "true"}),
        ("order", {"test": 1}),
        ("order", {"test": True, "status": "discarded"}),
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
