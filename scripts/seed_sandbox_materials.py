# scripts/seed_sandbox_materials.py
"""Crea unidades de insumo directo en buffer store vía el endpoint sandbox.
Uso: ajustar REQUIRED_MATERIALS según lo que necesites abastecer."""
import asyncio

from app.clients.farma_central import FarmaCentralClient

REQUIRED_MATERIALS = {
    "API-AMOXI-500": 36,
    "EXC-LACTOSA-DC": 24,
    "LAM-BLISTER-PVC": 3,
}


async def main():
    async with FarmaCentralClient() as client:
        for sku, count in REQUIRED_MATERIALS.items():
            for i in range(count):
                result = await client.post("/sandbox/products", {"sku": sku})
                print(f"{sku} [{i + 1}/{count}]:", result.get("productId"))


if __name__ == "__main__":
    asyncio.run(main())