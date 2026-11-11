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


def test_policy_cannot_disable_operational_buffers(monkeypatch):
    monkeypatch.setattr(module, "get_settings", lambda: make_settings())
    monkeypatch.setattr("sys.argv", ["sell_surplus", "--kit-buffer", "0"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
