import asyncio
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx
import pytest

from app.clients.llm_gateway import (
    LLMGatewayBudgetExceededError,
    LLMGatewayClient,
    LLMGatewayRateLimitError,
)
from app.clients.llm_gateway import rate_limit as module
from app.clients.llm_gateway.rate_limit import LLMGatewayRateLimiter
from tests.clients.test_llm_gateway import completion, propose, settings


def reserve_in_process(path):
    async def reserve():
        try:
            await LLMGatewayRateLimiter(Path(path)).acquire(20)
        except LLMGatewayRateLimitError:
            return False
        return True

    return asyncio.run(reserve())


def test_all_processes_share_one_token_window(tmp_path):
    path = tmp_path / "shared.rate"
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("fork")
    ) as pool:
        first = pool.submit(reserve_in_process, str(path))
        second = pool.submit(reserve_in_process, str(path))
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [
            False,
            True,
        ]
    assert json.loads(path.read_text())["requests"] == 1


@pytest.mark.anyio
async def test_actual_usage_and_budget_block_are_shared_across_restarts(
    tmp_path, monkeypatch
):
    path = tmp_path / "shared.rate"
    first = LLMGatewayRateLimiter(path)
    await first.acquire(20)
    await first.record(
        {"prompt_tokens": 300, "completion_tokens": 80, "total_tokens": 380},
        LLMGatewayBudgetExceededError(429),
    )
    monkeypatch.setattr(module.time, "time", lambda: 99999999999)
    with pytest.raises(LLMGatewayBudgetExceededError):
        await LLMGatewayRateLimiter(path).acquire(20)
    state = json.loads(path.read_text())
    assert state["requests"] == 1
    assert state["total_tokens"] == 380


@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["broken", "[]", '{"next_allowed": NaN}'])
async def test_corrupt_shared_state_does_not_reset_the_quota(tmp_path, raw):
    path = tmp_path / "shared.rate"
    path.write_text(raw)
    with pytest.raises(LLMGatewayRateLimitError):
        await LLMGatewayRateLimiter(path).acquire(20)
    assert path.read_text() == raw


@pytest.mark.anyio
async def test_quota_file_symlinks_are_not_followed(tmp_path):
    target = tmp_path / "private"
    target.write_text("unchanged")
    path = tmp_path / "shared.rate"
    path.symlink_to(target)
    with pytest.raises(LLMGatewayRateLimitError):
        await LLMGatewayRateLimiter(path).acquire(20)
    assert target.read_text() == "unchanged"


@pytest.mark.anyio
async def test_clients_with_different_keys_share_quota_and_audit_usage(tmp_path):
    path = tmp_path / "shared.rate"
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        first = LLMGatewayClient(
            settings=settings(llm_gateway_rate_limit_file=path), http_client=http_client
        )
        second = LLMGatewayClient(
            settings=settings(
                llm_gateway_rate_limit_file=path, llm_gateway_api_key="a-different-key"
            ),
            http_client=http_client,
        )
        await propose(first)
        with pytest.raises(LLMGatewayRateLimitError) as error:
            await propose(second)
    assert len(requests) == 1
    assert error.value.retry_after_seconds >= 60
    state = json.loads(path.read_text())
    assert state["total_tokens"] == 280
    assert "private-test-key" not in path.read_text()
    assert "a-different-key" not in path.read_text()


def test_default_quota_scope_includes_group_and_gateway_but_not_key():
    first = settings()
    second = settings(llm_gateway_api_key="different", llm_gateway_model="another")
    assert (
        first.effective_llm_gateway_rate_limit_file
        == second.effective_llm_gateway_rate_limit_file
    )
    assert (
        first.effective_llm_gateway_rate_limit_file
        != settings(farma_central_group=4).effective_llm_gateway_rate_limit_file
    )
