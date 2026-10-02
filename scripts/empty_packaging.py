import asyncio

from app.clients.farma_central import FarmaCentralClient
from scripts.farma_central_common import get_buffer_and_packaging

# Todos los SKUs que podríamos tener atorados
ALL_SKUS = [
    "API-AMOXI-500",
    "EXC-LACTOSA-DC",
    "LAM-BLISTER-PVC",
    "API-IBUPRO-400",
    "API-SALBUTA",
    "EXC-JARABE-BASE",
    "FRA-VIDRIO-120",
    "BLI-AMOXI-500",
    "BLI-IBUPRO-400",
    "FRA-SALBUTA-120",
]


async def main():
    async with FarmaCentralClient() as client:
        buffer_space, packaging_space = await get_buffer_and_packaging(client)
        print("Obteniendo productos en packaging y moviéndolos al buffer...")

        moved = 0
        for sku in ALL_SKUS:
            try:
                # Consultamos por cada SKU específicamente
                products = await client.get_space_products(
                    packaging_space["_id"], sku=sku
                )
                for p in products:
                    try:
                        await client.move_product(p["_id"], buffer_space["_id"])
                        moved += 1
                    except Exception as e:
                        print(f"Error moviendo {p['_id']}: {e}")
            except Exception:
                pass

        print(
            f"¡Listo! Se devolvieron {moved} productos al buffer. Packaging está vacío."
        )


if __name__ == "__main__":
    asyncio.run(main())
