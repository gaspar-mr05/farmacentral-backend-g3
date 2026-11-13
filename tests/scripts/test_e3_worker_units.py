from pathlib import Path

from scripts import process_billing, process_trades


def test_new_worker_entrypoints_reuse_the_tested_cycles():
    assert process_billing.run.__module__ == "scripts.reconcile_billing"
    assert process_trades.run.__module__ == "scripts.reconcile_trades"


def test_systemd_target_runs_only_the_new_financial_and_market_names():
    target = Path("deploy/farmacentral-workers.target").read_text()
    assert "farmacentral-worker@process_billing.service" in target
    assert "farmacentral-worker@process_trades.service" in target
    assert "farmacentral-worker@reconcile_billing.service" not in target
    assert "farmacentral-worker@reconcile_trades.service" not in target
