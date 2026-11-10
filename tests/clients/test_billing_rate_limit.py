import asyncio
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from app.clients.billing.exceptions import BillingRateLimitError
from app.clients.billing.rate_limit import BillingRateLimiter


def _acquire_in_another_process(path):
    async def acquire():
        await BillingRateLimiter(Path(path), 60).acquire()
        return time.time()

    return asyncio.run(acquire())


def test_rate_budget_is_shared_between_real_processes(tmp_path):
    path = tmp_path / "shared.rate"
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("fork")
    ) as pool:
        first = pool.submit(_acquire_in_another_process, str(path))
        second = pool.submit(_acquire_in_another_process, str(path))
        observed = sorted([first.result(timeout=10), second.result(timeout=10)])
    assert observed[1] - observed[0] >= 0.9


@pytest.mark.anyio
async def test_shared_cooldown_is_not_reset_by_another_limiter(tmp_path):
    path = tmp_path / "shared.rate"
    first, second = BillingRateLimiter(path, 45), BillingRateLimiter(path, 60)
    await first.defer(120)
    with pytest.raises(BillingRateLimitError) as error:
        await second.acquire()
    assert error.value.outcome_uncertain is False
    assert error.value.retry_after_seconds > 100


@pytest.mark.anyio
@pytest.mark.parametrize("content", ["NaN", "broken", "Infinity"])
async def test_corrupt_rate_file_fails_closed_without_silently_resetting(
    tmp_path, content
):
    path = tmp_path / "shared.rate"
    path.write_text(content)
    with pytest.raises(BillingRateLimitError):
        await BillingRateLimiter(path, 45).acquire()
    assert path.read_text() == content


@pytest.mark.anyio
async def test_rate_file_symlink_is_not_followed(tmp_path):
    target = tmp_path / "do-not-touch"
    target.write_text("unchanged")
    path = tmp_path / "shared.rate"
    path.symlink_to(target)
    with pytest.raises(BillingRateLimitError):
        await BillingRateLimiter(path, 45).acquire()
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("rate", [0, -1, 61])
def test_standalone_limiter_cannot_exceed_documented_quota(tmp_path, rate):
    with pytest.raises(ValueError):
        BillingRateLimiter(tmp_path / "shared.rate", rate)
