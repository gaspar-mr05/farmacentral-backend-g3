# scripts/produce_only.py
"""Produce directamente, asumiendo que ya hay insumos suficientes en packaging."""
import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce


async def main():
    session = SessionLocal()
    async with FarmaCentralClient() as client:
        run, supply = await produce(
            session,
            client=client,
            sku="BLI-AMOXI-500",
            quantity=3,
            input_units_by_lot={},
        )
        print("ProductionRun:", run.id)
        print("Supply:", supply)


if __name__ == "__main__":
    asyncio.run(main())