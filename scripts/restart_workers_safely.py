"""Restart workers only between replenishment cycles, holding their cycle lock."""

import subprocess
from time import monotonic, sleep

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.production.locking import ProductionBusyError, production_lock
from app.services.production.replenishment import replenishment_lock


def run(*, timeout_seconds=300, unit="farmacentral-workers.target"):
    if unit not in {
        "farmacentral-workers.target",
        "farmacentral-worker@replenish_inventory.service",
    }:
        raise ValueError("Unsupported service")
    settings = get_settings()
    deadline = monotonic() + timeout_seconds
    with SessionLocal() as session:
        while True:
            try:
                lock = (
                    replenishment_lock(session, settings.billing_period_id)
                    if settings.billing_period_id
                    else production_lock(session)
                )
                with lock:
                    subprocess.run(
                        [
                            "sudo",
                            "-n",
                            "/usr/bin/systemctl",
                            "restart",
                            unit,
                        ],
                        check=True,
                    )
                return
            except ProductionBusyError:
                if monotonic() >= deadline:
                    raise RuntimeError(
                        "Replenishment still busy; workers were not restarted"
                    ) from None
                sleep(1)


if __name__ == "__main__":
    run()
