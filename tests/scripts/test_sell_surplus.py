from unittest.mock import AsyncMock

import pytest

from scripts import sell_surplus as module
from tests.support.farma_central import make_settings


def test_cli_is_preview_only_by_default(monkeypatch):
    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    runner = AsyncMock()
    monkeypatch.setattr(module, "run", runner)
    monkeypatch.setattr("sys.argv", ["sell_surplus"])
    module.main()
    args = runner.await_args.kwargs
    assert args["execute"] is False
    assert args["policy"].skus == (
        "EXC-LACTOSA-DC",
        "EXC-SUERO-FISIO",
        "LAM-BLISTER-PVC",
    )
    assert args["policy"].stock_market is False


def test_market_stocking_requires_explicit_flag(monkeypatch):
    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    runner = AsyncMock()
    monkeypatch.setattr(module, "run", runner)
    monkeypatch.setattr("sys.argv", ["sell_surplus", "--stock-market"])

    module.main()

    assert runner.await_args.kwargs["policy"].stock_market is True


def test_production_lock_contention_retries_sooner():
    assert (
        module.next_delay(
            120,
            {"reason": "Producer currently holds the packaging lock"},
        )
        == 30
    )
    assert module.next_delay(120, {"reason": "Market stocking receipt pending"}) == 120


def test_policy_cannot_disable_operational_buffers(monkeypatch):
    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    monkeypatch.setattr("sys.argv", ["sell_surplus", "--kit-buffer", "0"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
