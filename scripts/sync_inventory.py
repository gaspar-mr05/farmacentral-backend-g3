import asyncio
import os
import tempfile
import time
from pathlib import Path

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.inventory.cold_chain import ColdChainService
from app.services.inventory.sync import InventorySyncService

MINIMUM_SYNC_INTERVAL_SECONDS = 300


def _marker_path() -> Path:
    configured = os.getenv("INVENTORY_SYNC_MARKER_FILE")
    if configured:
        return Path(configured)
    shared = Path("/var/lib/farmacentral")
    directory = (
        shared
        if shared.is_dir() and os.access(shared, os.W_OK)
        else Path(tempfile.gettempdir())
    )
    return directory / f"inventory-sync-{os.getuid()}.stamp"


def _recently_synchronized(
    marker: Path,
    *,
    now: float | None = None,
    interval: float = MINIMUM_SYNC_INTERVAL_SECONDS,
) -> bool:
    try:
        modified = marker.stat().st_mtime
    except FileNotFoundError:
        return False
    return (time.time() if now is None else now) - modified < interval


async def run() -> None:
    marker = _marker_path()
    if _recently_synchronized(marker):
        print("Inventory synchronization skipped: recent successful observation")
        return
    with SessionLocal() as session:
        async with FarmaCentralClient() as client:
            sync_service = InventorySyncService(client, session)
            # Production and trade workers also observe inventory. This periodic
            # pass exists primarily for cold-chain recovery, so tolerate groups
            # that change while the multi-request snapshot is being collected.
            result = await sync_service.synchronize(tolerate_changes=True)
            movements = await ColdChainService(
                client,
                session,
            ).relocate_exposed_units()

            if movements:
                await sync_service.synchronize(tolerate_changes=True)

    marker.touch()

    print(
        "Inventory synchronized: "
        f"products={result.products.created} created/"
        f"{result.products.updated} updated, "
        f"locations={result.locations.created} created/"
        f"{result.locations.updated} updated, "
        f"lots={result.lots.created} created/{result.lots.updated} updated, "
        f"units={result.units.created} created/{result.units.updated} updated, "
        f"cold_chain_movements={len(movements)}"
    )


if __name__ == "__main__":
    asyncio.run(run())
