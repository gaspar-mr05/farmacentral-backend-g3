import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.inventory.cold_chain import ColdChainService
from app.services.inventory.sync import InventorySyncService


async def run() -> None:
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
