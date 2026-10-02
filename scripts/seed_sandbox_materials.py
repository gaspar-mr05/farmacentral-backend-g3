# scripts/seed_sandbox_materials.py
"""Crea unidades de insumo vía el endpoint sandbox, respetando el rate-limit
general de 250 requests/60s."""

import asyncio

from app.clients.farma_central import FarmaCentralClient

REQUIRED_MATERIALS = {
    "API-AMOXI-500": 36,
    "API-IBUPRO-400": 40,
    "API-SALBUTA": 12,
    "EXC-LACTOSA-DC": 48,
    "LAM-BLISTER-PVC": 7,
    "EXC-JARABE-BASE": 24,
    "FRA-VIDRIO-120": 3,
}

BATCH_SIZE = 200  # requests por tanda, con margen bajo el límite de 250
PAUSE_SECONDS = 65  # más que la ventana de 60s, para asegurar el reset


async def main():
    async with FarmaCentralClient() as client:
        request_count = 0
        for sku, count in REQUIRED_MATERIALS.items():
            for i in range(count):
                result = await client.post("/sandbox/products", {"sku": sku})
                print(f"{sku} [{i + 1}/{count}]:", result.get("productId"))
                request_count += 1

                if request_count % BATCH_SIZE == 0:
                    print(
                        f"\nPausa de {PAUSE_SECONDS}s para respetar el rate-limit...\n"
                    )
                    await asyncio.sleep(PAUSE_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
