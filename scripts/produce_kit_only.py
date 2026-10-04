"""Produce el kit final, asumiendo que los productos acondicionados
intermedios (BLI-AMOXI-500, BLI-IBUPRO-400, FRA-SALBUTA-120) ya existen
en el área de acondicionamiento de Farma Central en cantidad suficiente.

Correr esto SOLO después de que los intermedios hayan pasado su propio
available_at y estén movidos a packaging (usa check_inventory.py para
confirmar antes de correr este script).
"""

import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce

KIT_SKU = "KIT-RESP-ADULTO"
KIT_QUANTITY = 1


async def main():
    session = SessionLocal()
    async with FarmaCentralClient() as client:
        print(f"Produciendo {KIT_QUANTITY} unidades de {KIT_SKU}...")
        try:
            run, supply = await produce(
                session,
                client=client,
                sku=KIT_SKU,
                quantity=KIT_QUANTITY,
            )
            print("¡Kit producido!")
            print("ProductionRun ID:", run.id)
            print("Available at:", supply.available_at)
        except Exception as e:
            print(f"Error en la producción del kit: {e}")


if __name__ == "__main__":
    asyncio.run(main())
