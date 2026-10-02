# scripts/full_flow.py
"""Ejecuta el flujo completo con un único cliente (una sola autenticación)."""

import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce
from scripts.farma_central_common import get_buffer_and_packaging

REQUIRED_MATERIALS = {
    "API-AMOXI-500": 36,
    "EXC-LACTOSA-DC": 24,
    "LAM-BLISTER-PVC": 3,
}


async def main():
    async with FarmaCentralClient() as client:  # una sola instancia, un solo token
        _, packaging_space = await get_buffer_and_packaging(client)

        created_ids: dict[str, list[str]] = {sku: [] for sku in REQUIRED_MATERIALS}
        for sku, count in REQUIRED_MATERIALS.items():
            for _ in range(count):
                result = await client.post("/sandbox/products", {"sku": sku})
                created_ids[sku].append(result["productId"])

        for _sku, ids in created_ids.items():
            for product_id in ids:
                await client.move_product(product_id, packaging_space["_id"])

        session = SessionLocal()
        run, supply = await produce(
            session,
            client=client,
            sku="BLI-AMOXI-500",
            quantity=3,
        )
        print("ProductionRun:", run.id)
        print("Supply:", supply)

        # espera hasta available_at antes de verificar
        wait_seconds = (
            supply.available_at
            - __import__("datetime").datetime.now(supply.available_at.tzinfo)
        ).total_seconds()
        if wait_seconds > 0:
            print(f"Esperando {wait_seconds:.0f}s hasta available_at...")
            await asyncio.sleep(wait_seconds + 5)

        products = await client.get_space_products(
            packaging_space["_id"], "BLI-AMOXI-500"
        )
        print("Lote de salida:", products)


if __name__ == "__main__":
    asyncio.run(main())
