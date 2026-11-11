from unittest.mock import AsyncMock

import pytest

from scripts import procure_scarce_supply as module


def test_cli_previews_by_default_without_purchase_authorization(monkeypatch):
    runner = AsyncMock()
    monkeypatch.setattr(module, "run", runner)
    monkeypatch.setattr(
        "sys.argv",
        [
            "procure_scarce_supply",
            "--sku",
            "KIT-CURACION",
            "--quantity",
            "2",
            "--deadline",
            "2026-11-20T18:00:00-03:00",
            "--expected-revenue",
            "100",
        ],
    )
    module.main()
    assert runner.await_args.kwargs["execute"] is False


def test_mutation_requires_stable_request_identity(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "procure_scarce_supply",
            "--sku",
            "KIT-CURACION",
            "--quantity",
            "2",
            "--deadline",
            "2026-11-20T18:00:00-03:00",
            "--expected-revenue",
            "100",
            "--execute",
        ],
    )
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
