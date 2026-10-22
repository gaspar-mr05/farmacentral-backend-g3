import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.clients.llm_gateway import LLMGatewayClient
from app.clients.llm_gateway_exceptions import (
    LLMGatewayBudgetExceededError,
    LLMGatewayConnectionError,
    LLMGatewayError,
    LLMGatewayHTTPError,
    LLMGatewayInvalidResponseError,
    LLMGatewayTimeoutError,
)
from app.schemas.llm_gateway import LLMCatalogItem
from tests.support.farma_central import make_settings

RECEIVED_AT = datetime(2026, 10, 27, 16, 5, tzinfo=UTC)
CATALOG = [LLMCatalogItem(sku="KIT-RESP-ADULTO", name="Kit respiratorio adulto")]
TEXT = "Necesito dos kits respiratorios para adulto, hoy antes de las 7 pm."


def settings(**overrides):
    return make_settings(
        **{
            "llm_gateway_base_url": "https://llm.test/course/v1/",
            "llm_gateway_api_key": "private-test-key",
            "llm_gateway_model": "course-model",
            "llm_gateway_timeout_seconds": 2,
            "llm_gateway_max_tokens": 400,
            **overrides,
        }
    )


def proposal(**overrides):
    return {
        "is_order": True,
        "sku": "KIT-RESP-ADULTO",
        "quantity": 2,
        "delivery_date": "2026-10-27T19:00:00-03:00",
        "confidence": 0.95,
        "is_ambiguous": False,
        "reason": "Pedido explícito de dos kits respiratorios de adulto.",
        **overrides,
    }


def completion(content=None, **choice_overrides):
    return {
        "id": "completion-test",
        "model": "course-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(proposal()) if content is None else content,
                },
                **choice_overrides,
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 80, "total_tokens": 280},
    }


async def propose(client):
    return await client.propose_order(
        text=TEXT, received_at=RECEIVED_AT, catalog=CATALOG
    )


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["", "Pedido explícito de dos kits."])
async def test_openai_compatible_contract_context_and_auditable_result(reason, caplog):
    requests = []
    raw = completion(json.dumps(proposal(reason=reason)))

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://llm.test/course/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        assert request.extensions["timeout"]["read"] == 2
        body = json.loads(request.content)
        assert body["model"] == "course-model"
        assert body["max_tokens"] == 400
        assert body["stream"] is False
        system, user = body["messages"]
        assert system["role"] == "system"
        assert "is_ambiguous=true" in system["content"]
        assert user["role"] == "user"
        assert json.loads(user["content"]) == {
            "text": TEXT,
            "received_at": "2026-10-27T13:05:00-03:00",
            "catalog": [{"sku": "KIT-RESP-ADULTO", "name": "Kit respiratorio adulto"}],
        }
        response_format = body["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        schema = response_format["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(proposal())
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        async with LLMGatewayClient(
            settings=settings(), http_client=http_client
        ) as client:
            result = await propose(client)
        assert not http_client.is_closed
    assert len(requests) == 1
    assert result.proposal.quantity == 2
    assert result.proposal.reason == reason
    assert result.proposal.delivery_date.astimezone(UTC).hour == 22
    assert result.raw_output == raw
    assert "private-test-key" not in caplog.text
    assert TEXT not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("is_order", [True, False])
async def test_model_can_express_ambiguity_or_non_order(is_order):
    raw = completion(
        json.dumps(
            proposal(
                is_order=is_order,
                sku=None,
                quantity=None,
                delivery_date=None,
                confidence=0.2,
                is_ambiguous=True,
                reason="No identifica el kit ni el plazo.",
            )
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
    ) as http_client:
        client = LLMGatewayClient(settings=settings(), http_client=http_client)
        result = await propose(client)
    assert result.proposal.is_order is is_order
    assert result.proposal.is_ambiguous
    assert result.proposal.sku is None


@pytest.mark.parametrize(
    "missing", ["llm_gateway_base_url", "llm_gateway_api_key", "llm_gateway_model"]
)
def test_missing_configuration_is_controlled(missing):
    with pytest.raises(LLMGatewayError, match="Set LLM_GATEWAY"):
        LLMGatewayClient(settings=settings(**{missing: None}))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ReadTimeout, LLMGatewayTimeoutError),
        (httpx.ConnectTimeout, LLMGatewayTimeoutError),
        (httpx.ConnectError, LLMGatewayConnectionError),
        (httpx.RemoteProtocolError, LLMGatewayConnectionError),
    ],
)
async def test_transport_errors_are_controlled_and_not_retried(failure, expected):
    requests = []

    def handler(request):
        requests.append(request)
        raise failure("sensitive transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMGatewayClient(settings=settings(), http_client=http_client)
        with pytest.raises(expected) as error:
            await propose(client)
    assert len(requests) == 1
    assert "sensitive" not in str(error.value)


@pytest.mark.anyio
async def test_total_timeout_also_bounds_a_stalled_transport():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMGatewayClient(
            settings=settings(llm_gateway_timeout_seconds=0.05),
            http_client=http_client,
        )
        with pytest.raises(LLMGatewayTimeoutError):
            await asyncio.wait_for(propose(client), timeout=2)
    assert started.is_set() and cancelled.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            400,
            {"error": {"message": "Budget has been exceeded!"}},
            LLMGatewayBudgetExceededError,
        ),
        (402, {}, LLMGatewayBudgetExceededError),
        (403, {"error": {"type": "budget_exceeded"}}, LLMGatewayBudgetExceededError),
        (
            429,
            {"error": {"message": "litellm.BudgetExceededError"}},
            LLMGatewayBudgetExceededError,
        ),
        (429, {"error": {"code": "insufficient_quota"}}, LLMGatewayBudgetExceededError),
        (429, {"error": {"type": "throttling_error"}}, LLMGatewayHTTPError),
        (400, {}, LLMGatewayHTTPError),
        (401, {}, LLMGatewayHTTPError),
        (403, {}, LLMGatewayHTTPError),
        (500, {}, LLMGatewayHTTPError),
        (503, {}, LLMGatewayHTTPError),
    ],
)
async def test_http_and_budget_errors_do_not_retry_or_leak_response(
    status, body, expected, caplog
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={**body, "private": "private-test-key"},
            headers={"Retry-After": "12"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMGatewayClient(settings=settings(), http_client=http_client)
        with pytest.raises(expected) as error:
            await propose(client)
    assert type(error.value) is expected
    assert error.value.status_code == status
    assert error.value.retry_after_seconds == (12 if status == 429 else None)
    assert "private-test-key" not in str(error.value) + caplog.text
    assert len(requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [
        [],
        None,
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [completion()["choices"][0]] * 2},
        completion(finish_reason="length"),
        completion(finish_reason="content_filter"),
        completion(message={"role": "assistant", "content": None}),
        completion(
            message={"role": "assistant", "refusal": "refused", "content": "{}"}
        ),
        completion(message={"role": "assistant", "content": {"quantity": 2}}),
        completion(message={"role": "user", "content": json.dumps(proposal())}),
        completion("not json"),
        completion("```json\n" + json.dumps(proposal()) + "\n```"),
        completion(json.dumps({"is_order": True})),
        completion(json.dumps(proposal(quantity="2"))),
        completion(json.dumps(proposal(quantity=True))),
        completion(json.dumps(proposal(quantity=1.5))),
        completion(json.dumps(proposal(quantity=0))),
        completion(json.dumps(proposal(quantity=-1))),
        completion(json.dumps(proposal(is_order="true"))),
        completion(json.dumps(proposal(is_ambiguous="false"))),
        completion(json.dumps(proposal(confidence=1.1))),
        completion(json.dumps(proposal(confidence=-0.1))),
        completion(json.dumps(proposal(confidence="0.9"))),
        completion(json.dumps(proposal(sku="  "))),
        completion(json.dumps(proposal(delivery_date="2026-10-27T19:00:00"))),
        completion(json.dumps(proposal(delivery_date="tomorrow"))),
        completion(json.dumps(proposal(unexpected="instruction"))),
    ],
)
async def test_invalid_proposals_preserve_raw_output_for_audit(raw):
    # Use explicit JSON null: httpx's json=None would send an empty body.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps(raw))
        )
    ) as http_client:
        client = LLMGatewayClient(settings=settings(), http_client=http_client)
        with pytest.raises(LLMGatewayInvalidResponseError) as error:
            await propose(client)
    assert error.value.raw_output == raw


@pytest.mark.anyio
async def test_non_json_and_unexpected_status_are_controlled():
    for status in (200, 204, 302):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, code=status: httpx.Response(
                    code, text="<html>unavailable</html>"
                )
            )
        ) as http_client:
            client = LLMGatewayClient(settings=settings(), http_client=http_client)
            with pytest.raises(LLMGatewayInvalidResponseError) as error:
                await propose(client)
        if status == 200:
            assert error.value.raw_output == "<html>unavailable</html>"


@pytest.mark.anyio
async def test_empty_catalog_prevents_any_gateway_call():
    def handler(request):
        pytest.fail("An empty catalog must not consume gateway budget")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = LLMGatewayClient(settings=settings(), http_client=http_client)
        with pytest.raises(LLMGatewayError, match="No clinical kits"):
            await client.propose_order(text=TEXT, received_at=RECEIVED_AT, catalog=[])


@pytest.mark.anyio
async def test_client_closes_the_http_client_it_created():
    client = LLMGatewayClient(settings=settings())
    async with client:
        assert not client._http_client.is_closed
    assert client._http_client.is_closed
