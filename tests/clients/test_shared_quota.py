import asyncio
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx
import pytest

from app.clients.billing import BillingClient
from app.clients.checkout import CheckoutClient
from app.clients.farma_central import FarmaCentralClient
from app.clients.market_prices import MarketPriceClient
from app.clients.messaging import MessagingClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.shared_quota import SharedQuotaTransport, _cooldown
from app.clients.trades import TradesClient
from tests.support.farma_central import make_settings


def _reserve_in_process(directory):
    async def reserve():
        transport = SharedQuotaTransport(3, directory=Path(directory))
        try:
            await transport._wait("prod.example.test")
            return time.time()
        finally:
            await transport.aclose()

    return asyncio.run(reserve())


def test_one_budget_is_shared_between_processes(tmp_path):
    with ProcessPoolExecutor(
        max_workers=3, mp_context=multiprocessing.get_context("fork")
    ) as pool:
        results = [pool.submit(_reserve_in_process, str(tmp_path)) for _ in range(3)]
        times = sorted(result.result(timeout=10) for result in results)
    assert all(b - a >= 0.25 for a, b in zip(times, times[1:], strict=False))


def test_group_and_host_have_separate_budgets(tmp_path):
    first = SharedQuotaTransport(3, directory=tmp_path)
    second = SharedQuotaTransport(4, directory=tmp_path)
    assert first._update("prod.example.test") == 0
    assert first._update("dev.example.test") == 0
    assert second._update("prod.example.test") == 0
    assert first._update("prod.example.test") > 0


@pytest.mark.anyio
async def test_429_cooldown_is_shared_without_replaying_post(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "10"})

    transport = SharedQuotaTransport(
        3, transport=httpx.MockTransport(handler), directory=tmp_path
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post("https://prod.example.test/orders/accept")
    assert response.status_code == 429
    assert len(calls) == 1
    other_worker = SharedQuotaTransport(3, directory=tmp_path)
    delay = other_worker._update("prod.example.test")
    assert 9 <= delay <= 10
    other_worker._update("prod.example.test", cooldown=1)
    assert other_worker._update("prod.example.test") >= 9


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["nan", "inf", "broken", "symlink"])
async def test_invalid_state_sends_no_request(tmp_path, invalid):
    calls = []
    transport = SharedQuotaTransport(
        3,
        transport=httpx.MockTransport(lambda request: calls.append(request)),
        directory=tmp_path,
    )
    transport._update("prod.example.test")
    path = next(tmp_path.glob("*.rate"))
    if invalid == "symlink":
        target = tmp_path / "untouched"
        target.write_text("original")
        path.unlink()
        path.symlink_to(target)
    else:
        path.write_text(invalid)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.ConnectError, match="no request sent"):
            await client.get("https://prod.example.test/orders")
    assert calls == []
    if invalid == "symlink":
        assert target.read_text() == "original"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "client_type",
    [
        FarmaCentralClient,
        PurchaseOrdersClient,
        TradesClient,
        MarketPriceClient,
        MessagingClient,
        CheckoutClient,
        BillingClient,
    ],
)
async def test_default_clients_use_shared_transport(client_type):
    async with client_type(settings=make_settings()) as client:
        assert isinstance(client._http_client._transport, SharedQuotaTransport)


@pytest.mark.parametrize("value", ["bad", "nan", "inf"])
def test_invalid_retry_after_falls_back_to_one_minute(value):
    assert _cooldown(httpx.Response(429, headers={"Retry-After": value})) == 60
