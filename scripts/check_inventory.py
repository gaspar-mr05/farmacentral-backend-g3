# scripts/check_inventory.py
"""Muestra cuántas unidades y con qué expiresAt hay de los SKUs indicados,
en cada espacio de la fábrica. Útil para diagnosticar vencimientos."""
import asyncio

from app.clients.farma_central import FarmaCentralClient

SKUS_TO_CHECK = [
    "API-AMOXI-500", "EXC-LACTOSA-DC", "LAM-BLISTER-PVC",  # para BLI-AMOXI-500
    "API-IBUPRO-400", "EXC-LACTOSA-DC",                     # para BLI-IBUPRO-400 (revisa fórmula real)
    "API-SALBUTA", "EXC-JARABE-BASE", "FRA-VIDRIO-120",     # para FRA-SALBUTA-120
    "BLI-AMOXI-500", "BLI-IBUPRO-400", "FRA-SALBUTA-120",   # intermedios
    "KIT-RESP-ADULTO",                                       # kit final
]

async def main():
    async with FarmaCentralClient() as client:
        spaces = await client.get_spaces()
        for space in spaces:
            for sku in SKUS_TO_CHECK:
                products = await client.get_space_products(space["_id"], sku)
                if products:
                    print(f"[{space['_id']}] {sku}: {len(products)} unidades")
                    for p in products[:2]:
                        print("  expiresAt:", p.get("expiresAt"))


if __name__ == "__main__":
    asyncio.run(main())