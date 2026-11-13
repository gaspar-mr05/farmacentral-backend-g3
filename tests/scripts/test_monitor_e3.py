import argparse
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from scripts import monitor_e3


@pytest.mark.parametrize("status,available", [(200, True), (503, False), (302, False)])
def test_probe_records_real_status_without_following_redirects(status, available):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        result = monitor_e3.probe(client, "https://site.test/")
    assert result["available"] is available
    assert result["status"] == status
    assert result["duration_ms"] >= 0


def test_timeout_preserves_failed_sample_without_raw_error():
    def fail(request):
        raise httpx.ReadTimeout("sensitive-detail")

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        result = monitor_e3.probe(client, "https://site.test/")
    assert not result["available"]
    assert result["error_type"] == "ReadTimeout"
    assert "sensitive-detail" not in str(result)


@pytest.mark.parametrize(
    "url",
    [
        "http://site.test",
        "https://user:secret@site.test/",
        "https://site.test/?key=secret",
    ],
)
def test_urls_must_not_leak_credentials(url):
    with pytest.raises(argparse.ArgumentTypeError):
        monitor_e3.public_url(url)


def test_availability_separates_evaluation_and_reports_missing_measurements(tmp_path):
    path = tmp_path / "observations.jsonl"
    period = uuid4()
    start = datetime(2026, 11, 18, 13, tzinfo=UTC)
    for seconds, scope, available in [
        (0, "outside_evaluation", True),
        (120, "evaluation", True),
        (480, "evaluation", False),
    ]:
        monitor_e3.append_report(
            path,
            {
                "period_id": str(period),
                "measurement_scope": scope,
                "observed_at": (start + timedelta(seconds=seconds)).isoformat(),
                "sites": {
                    "customer": {
                        "url": "https://site.test/",
                        "available": available,
                        "duration_ms": 10,
                    }
                },
            },
        )
    report = monitor_e3.summarize(path, period)
    evaluation = next(item for item in report["sites"] if item["scope"] == "evaluation")
    assert evaluation["observed_availability_ratio"] == 0.5
    assert evaluation["max_gap_seconds"] == 360
    assert evaluation["samples"] == 2
    assert monitor_e3.summarize(path, uuid4())["sites"] == []


def test_database_failure_does_not_lose_public_site_evidence(tmp_path, monkeypatch):
    period = uuid4()
    monkeypatch.setattr(
        monitor_e3,
        "get_settings",
        lambda: SimpleNamespace(billing_period_id=period, app_env="dev"),
    )
    from app.db import session

    monkeypatch.setattr(session, "engine", None)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    monkeypatch.setattr(monitor_e3.httpx, "Client", lambda **kwargs: client)
    path = tmp_path / "report.jsonl"
    assert (
        monitor_e3.main(
            [
                "--customer-url",
                "https://site.test/",
                "--traceability-url",
                "https://site.test/trazabilidad",
                "--output",
                str(path),
            ]
        )
        == 1
    )
    report = json.loads(path.read_text())
    assert report["database"]["status"] == "unavailable"
    assert len(report["sites"]) == 2
    assert report["measurement_scope"] == "unverified"
