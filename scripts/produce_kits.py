# scripts/produce_kits.py
"""Script para producir Kits Clínicos (ej. KIT-RESP-ADULTO) y cumplir

con el requisito de al menos 30 unidades producidas en dev.
"""
import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce


async def main():
    kit_sku = "KIT-RESP-ADULTO"  # Ajusta al SKU de kit correspondiente
    total_quantity = 30          # Requisito del paso 13

    async with FarmaCentralClient() as client:
        session = SessionLocal()
        print(f"Iniciando producción de {total_quantity} unidades del Kit {kit_sku}...")

        try:
            run, supply = await produce(
                session,
                client=client,
                sku=kit_sku,
                quantity=total_quantity,
            )
            print("¡Producción exitosa!")
            print("ProductionRun ID:", run.id)
            print("Available at:", supply.available_at)
        except Exception as e:
            print(f"Error en la producción del kit: {e}")


if __name__ == "__main__":
    asyncio.run(main())