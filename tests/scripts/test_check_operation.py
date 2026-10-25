import argparse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts import check_operation as module


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "https://user:key@test", "https://test/?key=secret"]
)
def test_probe_url_rejects_credentials_and_non_http(url):
    with pytest.raises(argparse.ArgumentTypeError):
        module.public_url(url)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["ok", "db-down", "bad-json", "timeout"])
async def test_http_probe_distinguishes_failure_without_exposing_body(mode):
    def handle(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("private-body")
        if mode == "db-down":
            return httpx.Response(503, text="private-body")
        if mode == "bad-json":
            return httpx.Response(200, text="private-body")
        return httpx.Response(200, json={"status": "ok", "database": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await module.check_http(
            client,
            "http://test/api/health/ready",
            expected={"status": "ok", "database": "ok"},
        )
    assert result["status"] == ("passed" if mode == "ok" else "failed")
    assert "private-body" not in str(result)


def test_disk_check_detects_insufficient_space_and_missing_volume(monkeypatch):
    monkeypatch.setattr(
        module.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(total=100 * 1024**3, free=2 * 1024**3),
    )
    assert module.check_disk(Path("/"), 1, 10)["status"] == "failed"
    assert module.check_disk(Path("/"), 1, 1)["status"] == "passed"

    def missing(_):
        raise FileNotFoundError

    monkeypatch.setattr(module.shutil, "disk_usage", missing)
    assert module.check_disk(Path("/missing"), 1, 1)["status"] == "failed"


@pytest.mark.anyio
async def test_report_runs_independent_checks_and_encodes_lot(monkeypatch):
    http = AsyncMock(return_value={"status": "passed"})
    monkeypatch.setattr(module, "check_http", http)
    monkeypatch.setattr(
        module, "check_systemd", AsyncMock(return_value={"status": "passed"})
    )
    monkeypatch.setattr(
        module, "check_workers", AsyncMock(return_value={"status": "failed"})
    )
    monkeypatch.setattr(module, "check_disk", lambda *_: {"status": "passed"})
    args = SimpleNamespace(
        api_url="http://localhost:8000",
        portal_url="https://portal.test",
        traceability_lot="batch/with space",
        check_services=True,
        include_replenishment=False,
        disk_path=None,
        minimum_free_gib=1,
        minimum_free_percent=10,
    )
    report = await module.run(args)
    assert report["checks"]["database"]["status"] == "passed"
    assert report["checks"]["workers"]["status"] == "failed"
    assert report["llm_calls"] == 0
    assert any("batch%2Fwith%20space" in call.args[1] for call in http.call_args_list)


@pytest.mark.anyio
@pytest.mark.parametrize("active", [True, False])
async def test_systemd_checks_api_supervision_and_timer(monkeypatch, active):
    state = "active" if active else "failed"
    data = (
        f"Id=farmacentral-backend.service\nLoadState=loaded\nActiveState={state}\n"
        "SubState=running\nRestart=on-failure\n\n"
        "Id=farmacentral-inventory-sync.timer\nLoadState=loaded\n"
        "ActiveState=active\nSubState=waiting\n"
    ).encode()
    process = SimpleNamespace(
        returncode=0, communicate=AsyncMock(return_value=(data, b""))
    )
    command = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", command)
    assert (await module.check_systemd())["status"] == (
        "passed" if active else "failed"
    )
    assert command.call_args.args[:2] == ("systemctl", "show")


def test_nonpassing_checks_produce_failure_exit(monkeypatch, capsys):
    monkeypatch.setattr(
        module,
        "run",
        AsyncMock(return_value={"checks": {"database": {"status": "failed"}}}),
    )
    assert module.main([]) == 1
    assert "database" in capsys.readouterr().out
