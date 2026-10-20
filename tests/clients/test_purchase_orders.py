import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.purchase_orders_exceptions import (
    PurchaseOrdersAuthenticationError,
    PurchaseOrdersConnectionError,
    PurchaseOrdersHTTPError,
    PurchaseOrdersInvalidResponseError,
    PurchaseOrdersTimeoutError,
)
from app.schemas.purchase_orders import PurchaseOrderState
from tests.support.farma_central import make_settings

ORDER_ID = "64abc123def456789012abcd"


def order_payload():
    return {
        "id": ORDER_ID,
        "cliente": "farmacia-externa",
        "proveedor": "3",
        "sku": "KIT-RESP-ADULTO",
        "cantidad": 2,
        "despachado": 0,
        "precioUnitario": 1500.25,
        "nombreCliente": "Farmacia de prueba",
        "estado": "creada",
        "historial": [{"nombre": "creada", "fecha": "2026-10-20T12:00:00Z"}],
        "creada": "2026-10-20T12:00:00Z",
        "actualizada": "2026-10-20T12:00:00Z",
        "vencimiento": "2026-10-20T14:00:00Z",
        "facturado": False,
    }


@pytest.mark.anyio
async def test_get_and_update_use_documented_contract_and_reuse_token(caplog):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/ordenes-compra/autenticar":
            assert json.loads(request.content) == {"group": 3, "secret": "test-secret"}
            return httpx.Response(200, json={"token": "test-token"})
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "GET":
            assert request.url.path == f"/ordenes-compra/ordenes/{ORDER_ID}"
            return httpx.Response(200, json=order_payload())
        assert request.url.path == f"/ordenes-compra/ordenes/{ORDER_ID}/estado"
        assert json.loads(request.content) == {"estado": "aceptada"}
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="https://orders.test/ordenes-compra/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        async with PurchaseOrdersClient(
            settings=make_settings(), http_client=http_client
        ) as client:
            order = await client.get_order(ORDER_ID.upper())
            await client.update_order_state(order.id, "aceptada")
        assert not http_client.is_closed

    assert len(requests) == 3
    assert order.customer_id == "farmacia-externa"
    assert order.supplier_id == "3"
    assert order.quantity == 2
    assert order.unit_price == Decimal("1500.25")
    assert order.state is PurchaseOrderState.CREATED
    assert order.delivery_deadline.utcoffset().total_seconds() == 0
    assert "test-secret" not in caplog.text
    assert "test-token" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["aceptada", "rechazada", "anulada"])
async def test_update_accepts_only_explicit_decisions(state):
    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        assert json.loads(request.content) == {"estado": state}
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        await client.update_order_state(ORDER_ID, state)


@pytest.mark.anyio
async def test_invalid_id_or_system_managed_state_never_sends_a_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(ValidationError):
            await client.get_order("../../another-service")
        with pytest.raises(ValidationError):
            await client.update_order_state(ORDER_ID, "cumplida")
    assert not requests


@pytest.mark.anyio
async def test_concurrent_requests_share_authentication_and_refresh_once():
    auth_count = 0
    expired_requests = 0
    both_expired = asyncio.Event()

    async def handler(request):
        nonlocal auth_count, expired_requests
        if request.url.path.endswith("/autenticar"):
            auth_count += 1
            return httpx.Response(200, json={"token": f"token-{auth_count}"})
        if request.headers["Authorization"] == "Bearer token-1":
            expired_requests += 1
            if expired_requests == 2:
                both_expired.set()
            await asyncio.wait_for(both_expired.wait(), timeout=2)
            return httpx.Response(401)
        return httpx.Response(200, json=order_payload())

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        orders = await asyncio.gather(
            client.get_order(ORDER_ID), client.get_order(ORDER_ID)
        )
    assert auth_count == 2
    assert all(order.id == ORDER_ID for order in orders)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_rejected_credentials_raise_authentication_error_without_logging_secrets(
    status, caplog
):
    def handler(request):
        return httpx.Response(status, json={"message": "test-secret test-token"})

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(PurchaseOrdersAuthenticationError) as error:
            await client.get_order(ORDER_ID)
    assert error.value.status_code == status
    assert "test-secret" not in str(error.value) + caplog.text
    assert "test-token" not in str(error.value) + caplog.text


@pytest.mark.anyio
async def test_repeated_unauthorized_response_stops_after_one_refresh():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "rejected"})
        return httpx.Response(401)

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(PurchaseOrdersAuthenticationError):
            await client.get_order(ORDER_ID)
    assert len(requests) == 4


@pytest.mark.anyio
@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
async def test_failed_state_update_preserves_status_and_does_not_retry(status, caplog):
    updates = []

    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        updates.append(request)
        return httpx.Response(status, headers={"Retry-After": "12"})

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(PurchaseOrdersHTTPError) as error:
            await client.update_order_state(ORDER_ID, "rechazada")
    assert len(updates) == 1
    assert error.value.status_code == status
    assert error.value.retry_after_seconds == (12 if status == 429 else None)
    assert ORDER_ID in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("transport_error", "expected"),
    [
        (httpx.ReadTimeout, PurchaseOrdersTimeoutError),
        (httpx.ConnectError, PurchaseOrdersConnectionError),
        (httpx.RemoteProtocolError, PurchaseOrdersConnectionError),
    ],
)
async def test_transport_failures_are_reported_without_repeating_state_update(
    transport_error, expected
):
    updates = []

    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        updates.append(request)
        raise transport_error("simulated failure", request=request)

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(expected):
            await client.update_order_state(ORDER_ID, "aceptada")
    assert len(updates) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {**order_payload(), "estado": "unknown"},
        {**order_payload(), "cantidad": 0},
        {**order_payload(), "vencimiento": "2026-10-20T14:00:00"},
        {**order_payload(), "id": "64abc123def456789012abce"},
    ],
)
async def test_invalid_order_responses_raise_integration_error(payload):
    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(PurchaseOrdersInvalidResponseError):
            await client.get_order(ORDER_ID)


@pytest.mark.anyio
@pytest.mark.parametrize("auth_payload", [{}, {"token": ""}, {"token": 123}])
async def test_authentication_requires_a_nonempty_token(auth_payload):
    def handler(request):
        return httpx.Response(200, json=auth_payload)

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(PurchaseOrdersInvalidResponseError):
            await client.get_order(ORDER_ID)


@pytest.mark.anyio
async def test_invalid_json_is_reported_as_an_integration_error():
    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, text="<html>Service failed</html>")

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(PurchaseOrdersInvalidResponseError, match="invalid JSON"):
            await client.get_order(ORDER_ID)


@pytest.mark.anyio
async def test_state_update_requires_documented_success_status():
    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, json={"success": False})

    async with httpx.AsyncClient(
        base_url="https://orders.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = PurchaseOrdersClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(PurchaseOrdersInvalidResponseError, match="unexpected HTTP"):
            await client.update_order_state(ORDER_ID, "aceptada")
