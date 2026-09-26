import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.inventory_sync import InventorySyncService


async def run() -> None:
    with SessionLocal() as session:
        async with FarmaCentralClient() as client:
            result = await InventorySyncService(client, session).synchronize()

    print(
        "Inventory synchronized: "
        f"products={result.products.created} created/"
        f"{result.products.updated} updated, "
        f"locations={result.locations.created} created/"
        f"{result.locations.updated} updated, "
        f"lots={result.lots.created} created/{result.lots.updated} updated, "
        f"units={result.units.created} created/{result.units.updated} updated"
    )


if __name__ == "__main__":
    asyncio.run(run())
