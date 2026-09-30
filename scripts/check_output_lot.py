# scripts/check_output_lot.py
"""Busca el SKU de salida indicado en todos los espacios, para confirmar
que Farma Central ya generó el lote tras una producción."""
import asyncio
import sys

from app.clients.farma_central import FarmaCentralClient


async def main(sku: str):
    async with FarmaCentralClient() as client:
        spaces = await client.get_spaces()
        for space in spaces:
            products = await client.get_space_products(space["_id"], sku)
            if products:
                print(f"[{space['_id']}] {sku}:")
                for p in products:
                    print(" ", p)


if __name__ == "__main__":
    sku = sys.argv[1] if len(sys.argv) > 1 else "BLI-AMOXI-500"
    asyncio.run(main(sku))