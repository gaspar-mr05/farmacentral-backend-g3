import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.clients.billing import (
    BillingAuthenticationError,
    BillingClient,
    BillingConfigurationError,
    BillingConnectionError,
    BillingHTTPError,
    BillingInvalidResponseError,
    BillingSOAPFault,
    BillingTimeoutError,
    soap,
)
from app.models.billing import InvoiceSide, InvoiceStatus
from tests.support.billing_soap import (
    INVOICE_ID,
    ORDER_ID,
    WSDL,
    fault,
    invoice_values,
    response,
)
from tests.support.farma_central import make_settings

NOW = datetime(2026, 11, 9, 16, tzinfo=UTC)


@pytest.fixture
def limiter():
    return AsyncMock()


@pytest.mark.anyio
@pytest.mark.parametrize("qualified", [True, False])
async def test_all_four_soap_operations_with_wsse_and_exact_money(limiter, qualified):
    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "GET":
            assert request.url.query in {b"wsdl", b"wsdl="}
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=WSDL)
        root = soap.xml_root(request.content)
        assert root.find(f".//{{{soap.WSSE}}}Username").text == "3"
        assert root.find(f".//{{{soap.WSSE}}}Password").text == "<&private-secret>"
        operation = request.headers["SOAPAction"].strip('"')
        assert (
            root.find(f"{{{soap.SOAP}}}Body")[0].tag
            == f"{{{soap.BILLING}}}{operation}Request"
        )
        if operation == "getBankStatement":
            items = [{"group": "3", "balance": "-0.123456789"}]
        elif operation == "emitInvoice":
            assert root.find(f".//{{{soap.BILLING}}}order_id").text == ORDER_ID
            items = [invoice_values(client="4", supplier="3")]
        elif operation == "payInvoice":
            assert root.find(f".//{{{soap.BILLING}}}invoice_id").text == INVOICE_ID
            items = [invoice_values(status="paid")]
        else:
            assert root.find(f".//{{{soap.BILLING}}}side").text == "client"
            assert root.find(f".//{{{soap.BILLING}}}status").text == "pending"
            items = [invoice_values()]
        return httpx.Response(
            200, content=response(operation, items, qualified=qualified)
        )

    settings = make_settings(farma_central_api_secret="<&private-secret>")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        async with BillingClient(
            settings=settings, http_client=http, limiter=limiter
        ) as client:
            statement = await client.get_bank_statement()
            invoices = await client.get_invoices(
                status=InvoiceStatus.PENDING,
                side=InvoiceSide.CLIENT,
                from_date=NOW - timedelta(days=1),
                to_date=NOW,
            )
            issued = await client.emit_invoice(ORDER_ID)
            paid = await client.pay_invoice(INVOICE_ID)
        assert not http.is_closed
    assert statement.balance == Decimal("-0.123456789")
    assert invoices[0].principal == Decimal("100.123456789")
    assert invoices[0].interest == Decimal("0.000000001")
    assert invoices[0].total == Decimal("100.123456790")
    assert issued.supplier_id == "3"
    assert paid.status is InvoiceStatus.PAID
    assert len(seen) == limiter.acquire.await_count == 5  # One cached WSDL, four RPCs.


@pytest.mark.anyio
@pytest.mark.parametrize("side", list(InvoiceSide))
@pytest.mark.parametrize("status", list(InvoiceStatus))
async def test_listing_accepts_empty_results_by_side_and_status(limiter, side, status):
    def handle(request):
        return httpx.Response(
            200,
            content=WSDL if request.method == "GET" else response("getInvoices", []),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        assert (
            await client.get_invoices(
                status=status, side=side, from_date=NOW - timedelta(days=3), to_date=NOW
            )
            == []
        )


@pytest.mark.anyio
async def test_concurrent_reads_load_wsdl_once_without_blocking_event_loop(limiter):
    entered, release = asyncio.Event(), asyncio.Event()
    gets = 0

    async def handle(request):
        nonlocal gets
        if request.method == "GET":
            gets += 1
            entered.set()
            await release.wait()
            return httpx.Response(200, content=WSDL)
        return httpx.Response(
            200, content=response("getBankStatement", [{"group": "3", "balance": "0"}])
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        first = asyncio.create_task(client.get_bank_statement())
        await entered.wait()
        second = asyncio.create_task(client.get_bank_statement())
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    assert gets == 1


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["emit_invoice", "pay_invoice"])
@pytest.mark.parametrize(
    "status,uncertain",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (409, False),
        (429, False),
        (500, True),
        (502, True),
        (302, True),
    ],
)
async def test_http_errors_never_retry_mutations_or_expose_body(
    limiter, method, status, uncertain
):
    posts = 0

    def handle(request):
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(200, content=WSDL)
        posts += 1
        return httpx.Response(
            status,
            text="private-response-and-secret",
            headers={"Retry-After": "12", "Location": "https://other.test/"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(BillingHTTPError) as error:
            await getattr(client, method)(
                ORDER_ID if method == "emit_invoice" else INVOICE_ID
            )
    assert posts == 1
    assert error.value.status_code == status
    assert error.value.outcome_uncertain is uncertain
    assert "private-response-and-secret" not in str(error.value)
    if status in {401, 403}:
        assert isinstance(error.value, BillingAuthenticationError)
    if status == 429:
        limiter.defer.assert_awaited_once_with(12)
        assert error.value.retry_after_seconds == 12


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["get_bank_statement", "emit_invoice"])
@pytest.mark.parametrize("http_status", [200, 500])
@pytest.mark.parametrize(
    "code,uncertain",
    [("Client", False), ("Server", True), ("unexpected-private", True)],
)
async def test_soap_fault_is_handled_even_on_http_200_without_echoing_fault_text(
    limiter, operation, code, uncertain, caplog, http_status
):
    def handle(request):
        return httpx.Response(
            200 if request.method == "GET" else http_status,
            content=WSDL
            if request.method == "GET"
            else fault(code, "private-server-password"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(BillingSOAPFault) as error:
            await (
                client.get_bank_statement()
                if operation == "get_bank_statement"
                else client.emit_invoice(ORDER_ID)
            )
    assert error.value.outcome_uncertain is (uncertain and operation == "emit_invoice")
    assert "private-server-password" not in str(error.value) + caplog.text
    assert "unexpected-private" not in str(error.value) + caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error_class,uncertain",
    [
        (httpx.ConnectTimeout, False),
        (httpx.PoolTimeout, False),
        (httpx.ReadTimeout, True),
        (httpx.WriteTimeout, True),
        (httpx.ConnectError, False),
        (httpx.ReadError, True),
    ],
)
async def test_transport_error_tracks_possible_effect_without_retry(
    limiter, error_class, uncertain
):
    posts = 0

    def handle(request):
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(200, content=WSDL)
        posts += 1
        raise error_class("private-transport", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises((BillingTimeoutError, BillingConnectionError)) as error:
            await client.pay_invoice(INVOICE_ID)
    assert posts == 1 and error.value.outcome_uncertain is uncertain
    assert "private-transport" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [
        b"not-xml",
        b"<html>private-body</html>",
        b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>',
    ],
)
async def test_invalid_mutation_response_is_uncertain_and_not_repeated(
    limiter, content
):
    posts = 0

    def handle(request):
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(200, content=WSDL)
        posts += 1
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(BillingInvalidResponseError) as error:
            await client.emit_invoice(ORDER_ID)
    assert error.value.outcome_uncertain and posts == 1
    assert "private-body" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "values",
    [
        invoice_values(price="NaN"),
        invoice_values(interest="-1"),
        invoice_values(createdAt="2026-11-09T10:00:00"),
        invoice_values(client="4"),
        invoice_values(status="paid"),
    ],
)
async def test_invalid_or_wrong_party_invoice_listing_is_not_accepted(limiter, values):
    def handle(request):
        return httpx.Response(
            200,
            content=WSDL
            if request.method == "GET"
            else response("getInvoices", [values]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(BillingInvalidResponseError):
            await client.get_invoices(
                status=InvoiceStatus.PENDING,
                side=InvoiceSide.CLIENT,
                from_date=NOW - timedelta(days=1),
                to_date=NOW,
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides", [{"client": "4"}, {"status": "pending"}, {"id": "other-invoice"}]
)
async def test_payment_result_must_confirm_requested_invoice_and_group(
    limiter, overrides
):
    def handle(request):
        return httpx.Response(
            200,
            content=WSDL
            if request.method == "GET"
            else response("payInvoice", [invoice_values(status="paid") | overrides]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(BillingInvalidResponseError) as error:
            await client.pay_invoice(INVOICE_ID)
    assert error.value.outcome_uncertain


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.test/soap",
        "https://user:private@billing.test/",
        "https://billing.test/?key=private",
    ],
)
def test_endpoint_configuration_rejects_plaintext_and_url_credentials(url):
    with pytest.raises(BillingConfigurationError) as error:
        BillingClient(settings=make_settings(billing_url=url))
    assert "private" not in str(error.value)


@pytest.mark.anyio
async def test_configured_prod_endpoint_overrides_dev_address_in_wsdl(limiter):
    urls = []

    def handle(request):
        urls.append(request.url)
        return httpx.Response(
            200,
            content=WSDL
            if request.method == "GET"
            else response("getBankStatement", [{"group": "3", "balance": "0"}]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(
                farma_central_base_url="https://prod.example.test/farma-central/"
            ),
            http_client=http,
            limiter=limiter,
        )
        await client.get_bank_statement()
    assert all(url.host == "prod.example.test" for url in urls)


@pytest.mark.anyio
async def test_invalid_request_window_fails_before_network_or_budget_use(limiter):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected request"))
    ) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(ValidationError):
            await client.get_invoices(
                status=InvoiceStatus.PENDING,
                side=InvoiceSide.CLIENT,
                from_date=NOW - timedelta(days=4),
                to_date=NOW,
            )
    limiter.acquire.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    [b"not-wsdl", WSDL.replace(b"emitInvoice", b"unknownOperation")],
)
async def test_invalid_wsdl_prevents_any_mutation_request(limiter, content):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(BillingInvalidResponseError) as error:
            await client.emit_invoice(ORDER_ID)
    assert [request.method for request in requests] == ["GET"]
    assert error.value.operation == "load_wsdl"
    assert error.value.outcome_uncertain is False


@pytest.mark.anyio
async def test_bank_statement_for_another_group_is_rejected(limiter):
    def handle(request):
        return httpx.Response(
            200,
            content=WSDL
            if request.method == "GET"
            else response("getBankStatement", [{"group": "4", "balance": "100"}]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BillingClient(
            settings=make_settings(), http_client=http, limiter=limiter
        )
        with pytest.raises(BillingInvalidResponseError) as error:
            await client.get_bank_statement()
    assert error.value.outcome_uncertain is False
