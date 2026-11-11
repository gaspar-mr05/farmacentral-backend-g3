import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.clients.trades import (
    TradesClient,
    TradesHTTPError,
    TradesInvalidResponseError,
    TradesUncertainResultError,
)
from app.schemas.trades import TradeOfferCreate
from tests.support.farma_central import make_settings

OFFER_ID = "64abc123def456789012abcd"
ORDER_ID = "64abc123def456789012abce"


def offer_payload(**overrides):
    payload = {
        "id": OFFER_ID,
        "side": "sell",
        "sku": "EXC-LACTOSA-DC",
        "quantity": 20,
        "unitPrice": 6,
        "state": "open",
        "publisher": "3",
        "counterparty": None,
        "closedAt": None,
        "createdAt": "2026-11-11T00:00:00Z",
        "expiresAt": "2026-11-11T01:00:00Z",
    }
    payload.update(overrides)
    return payload


@pytest.mark.anyio
async def test_offer_operations_follow_the_documented_contract_and_reuse_token():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/trades/autenticar":
            assert json.loads(request.content) == {"group": 3, "secret": "test-secret"}
            return httpx.Response(200, json={"success": True, "token": "test-token"})

        if request.method == "GET":
            assert "Authorization" not in request.headers
        else:
            assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "GET" and request.url.path == "/trades/offers":
            assert dict(request.url.params) == {
                "sku": "EXC-LACTOSA-DC",
                "side": "sell",
                "state": "open",
            }
            return httpx.Response(200, json=[offer_payload()])
        if request.method == "GET":
            return httpx.Response(200, json=offer_payload())
        if request.url.path == "/trades/offers":
            assert json.loads(request.content) == {
                "side": "sell",
                "sku": "EXC-LACTOSA-DC",
                "quantity": 20,
                "unitPrice": 6.0,
            }
            return httpx.Response(201, json=offer_payload())
        if request.url.path.endswith("/accept"):
            return httpx.Response(
                200,
                json=offer_payload(
                    state="closed",
                    orderId=ORDER_ID,
                    counterparty="4",
                    closedAt="2026-11-11T00:10:00Z",
                ),
            )
        return httpx.Response(200, json=offer_payload(state="cancelled"))

    async with httpx.AsyncClient(
        base_url="https://trades.test/trades/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = TradesClient(settings=make_settings(), http_client=http_client)
        offers = await client.list_offers(
            sku="EXC-LACTOSA-DC", side="sell", state="open"
        )
        detail = await client.get_offer(OFFER_ID.upper())
        created = await client.create_offer(
            TradeOfferCreate(
                side="sell",
                sku="EXC-LACTOSA-DC",
                quantity=20,
                unitPrice=Decimal("6"),
            ),
            current_unit_price=Decimal("5"),
        )
        accepted = await client.accept_offer(OFFER_ID)
        cancelled = await client.cancel_offer(OFFER_ID)

    assert len(requests) == 6
    assert offers[0].id == detail.id == created.id == OFFER_ID
    assert detail.lifetime.total_seconds() == 3600
    assert accepted.order_id == ORDER_ID
    assert cancelled.status == "cancelled"
    assert detail.publisher_group == 3
    assert accepted.counterparty_group == 4
    assert accepted.closed_at.isoformat() == "2026-11-11T00:10:00+00:00"


def test_offer_request_validates_side_quantity_and_price():
    with pytest.raises(ValidationError):
        TradeOfferCreate(side="other", sku="SKU", quantity=1, unitPrice=1)
    with pytest.raises(ValidationError):
        TradeOfferCreate(side="buy", sku="SKU", quantity=0, unitPrice=1)
    with pytest.raises(ValidationError):
        TradeOfferCreate(side="buy", sku="SKU", quantity=1, unitPrice=0)


@pytest.mark.anyio
async def test_price_outside_market_range_is_rejected_before_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        base_url="https://trades.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = TradesClient(settings=make_settings(), http_client=http_client)
        request = TradeOfferCreate(
            side="buy", sku="SKU", quantity=1, unitPrice=Decimal("16")
        )
        with pytest.raises(ValueError, match="0.5x and 1.5x"):
            await client.create_offer(request, current_unit_price=Decimal("10"))

    assert requests == []


@pytest.mark.anyio
async def test_invalid_offer_id_is_rejected_before_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        base_url="https://trades.test/", transport=httpx.MockTransport(handler)
    ) as http_client:
        client = TradesClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(ValidationError):
            await client.accept_offer("../../other-service")

    assert requests == []


@pytest.mark.anyio
async def test_rate_limit_is_exposed_without_repeating_mutation():
    mutations = []

    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        mutations.append(request)
        return httpx.Response(429, headers={"Retry-After": "12"})

    async with httpx.AsyncClient(
        base_url="https://trades.test/trades/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = TradesClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(TradesHTTPError) as error:
            await client.cancel_offer(OFFER_ID)

    assert len(mutations) == 1
    assert error.value.status_code == 429
    assert error.value.retry_after_seconds == 12


@pytest.mark.anyio
async def test_lost_mutation_response_is_reported_as_uncertain():
    mutations = []

    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        mutations.append(request)
        raise httpx.ReadTimeout("response lost", request=request)

    async with httpx.AsyncClient(
        base_url="https://trades.test/trades/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = TradesClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(TradesUncertainResultError, match="reconcile"):
            await client.accept_offer(OFFER_ID)

    assert len(mutations) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        offer_payload(side="unknown"),
        offer_payload(quantity=0),
        offer_payload(id="not-an-id"),
    ],
)
async def test_invalid_offer_responses_raise_integration_error(payload):
    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="https://trades.test/trades/",
        transport=httpx.MockTransport(handler),
    ) as http_client:
        client = TradesClient(settings=make_settings(), http_client=http_client)
        with pytest.raises(TradesInvalidResponseError):
            await client.get_offer(OFFER_ID)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["create", "accept", "cancel"])
@pytest.mark.parametrize("code", [500, 502, 408, 201])
async def test_mutation_with_server_error_or_invalid_success_is_uncertain(
    operation, code
):
    mutations = []

    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test"})
        mutations.append(request)
        return httpx.Response(code, content=b"invalid-json")

    async with httpx.AsyncClient(
        base_url="https://trades.test/trades/", transport=httpx.MockTransport(handler)
    ) as http:
        client = TradesClient(settings=make_settings(), http_client=http)
        with pytest.raises(TradesUncertainResultError):
            if operation == "create":
                await client.create_offer(
                    TradeOfferCreate(side="buy", sku="SKU", quantity=1, unitPrice=1),
                    current_unit_price=Decimal(1),
                )
            elif operation == "accept":
                await client.accept_offer(OFFER_ID)
            else:
                await client.cancel_offer(OFFER_ID)
    assert len(mutations) == 1


@pytest.mark.anyio
async def test_public_listing_needs_no_authentication():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/trades/offers"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=[offer_payload()])

    async with httpx.AsyncClient(
        base_url="https://trades.test/trades/", transport=httpx.MockTransport(handler)
    ) as http:
        client = TradesClient(settings=make_settings(), http_client=http)
        assert len(await client.list_offers()) == 1
    assert len(requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes",
    [
        {"state": "closing", "orderId": ORDER_ID},
        {"state": "closed", "orderId": ORDER_ID},
    ],
)
async def test_incomplete_acceptance_evidence_is_uncertain(changes):
    def handler(request):
        if request.url.path.endswith("/autenticar"):
            return httpx.Response(200, json={"token": "test-token"})
        return httpx.Response(200, json=offer_payload(**changes))

    async with httpx.AsyncClient(
        base_url="https://trades.test/trades/", transport=httpx.MockTransport(handler)
    ) as http:
        client = TradesClient(settings=make_settings(), http_client=http)
        with pytest.raises(TradesUncertainResultError):
            await client.accept_offer(OFFER_ID)
