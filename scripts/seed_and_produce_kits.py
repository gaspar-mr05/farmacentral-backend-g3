# scripts/seed_and_produce_kits.py
"""Abastece insumos y produce de forma multinivel los componentes requeridos

para el KIT-RESP-ADULTO antes de fabricar las 30 unidades del Kit.
"""

import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce
from scripts.farma_central_common import get_buffer_and_packaging

# Insumos directos de sandbox necesarios para armar los blísteres y frascos requeridos
REQUIRED_RAW_MATERIALS = {
    "API-AMOXI-500": 36,  # Para fabricar los blísteres de amoxicilina faltantes
    "API-IBUPRO-400": 60,  # Para fabricar los blísteres de ibuprofeno
    "API-SALBUTA-120": 30,  # Para los frascos de salbutamol
    "LAM-BLISTER-PVC": 30,
    "EXC-LACTOSA-DC": 30,
}


async def main():
    async with FarmaCentralClient() as client:
        _, packaging_space = await get_buffer_and_packaging(client)

        print("1. Sembrando materias primas en Sandbox...")
        for sku, count in REQUIRED_RAW_MATERIALS.items():
            for _ in range(count):
                res = await client.post("/sandbox/products", {"sku": sku})
                product_id = res["productId"]
                await client.move_product(product_id, packaging_space["_id"])

        print("2. Insumos primarios listos en acondicionamiento (packaging).")

        session = SessionLocal()

        # Producir los componentes acondicionados faltantes
        print("3. Produciendo componentes intermedios...")
        for comp_sku, qty in [
            ("BLI-AMOXI-500", 18),
            ("BLI-IBUPRO-400", 30),
            ("FRA-SALBUTA-120", 30),
        ]:
            print(f"   Fabricando {qty} unidades de {comp_sku}...")
            await produce(session, client=client, sku=comp_sku, quantity=qty)

        print("4. Componentes intermedios fabricados con éxito.")

        # Producir el Kit Final
        print("5. Iniciando fabricación de 30 unidades del KIT-RESP-ADULTO...")
        run, supply = await produce(
            session,
            client=client,
            sku="KIT-RESP-ADULTO",
            quantity=30,
        )

        print(f"¡ÉXITO TOTAL! ProductionRun ID: {run.id}")
        print(f"Lote disponible en Farma Central a las: {supply.available_at}")


if __name__ == "__main__":
    asyncio.run(main())
