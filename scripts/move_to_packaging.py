"""Mueve todas las unidades de los SKUs indicados desde buffer hacia el
área de acondicionamiento. Correr inmediatamente después de seed_sandbox_materials.py
para minimizar el riesgo de vencimiento."""

import asyncio

from app.clients.farma_central import FarmaCentralClient
from scripts.farma_central_common import get_buffer_and_packaging

REQUIRED_SKUS = ["API-AMOXI-500", "EXC-LACTOSA-DC", "LAM-BLISTER-PVC"]


async def main():
    async with FarmaCentralClient() as client:
        buffer_space, packaging_space = await get_buffer_and_packaging(client)

        for sku in REQUIRED_SKUS:
            products = await client.get_space_products(buffer_space["_id"], sku)
            print(f"{sku}: {len(products)} unidades en buffer")
            for product in products:
                await client.move_product(product["_id"], packaging_space["_id"])
            print(f"{sku}: movidas al área de acondicionamiento")


if __name__ == "__main__":
    asyncio.run(main())
