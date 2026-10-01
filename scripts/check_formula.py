# scripts/check_formula.py
import asyncio
from app.clients.farma_central import FarmaCentralClient

SKUS = ["BLI-AMOXI-500", "BLI-IBUPRO-400", "FRA-SALBUTA-120", "KIT-RESP-ADULTO"]

async def main():
    async with FarmaCentralClient() as client:
        catalog = await client.get_available_products()
        for p in catalog:
            if p["sku"] in SKUS:
                print(p["sku"], p)

asyncio.run(main())