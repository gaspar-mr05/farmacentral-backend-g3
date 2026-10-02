"""Script para reiniciar completamente el inventario:

1. Obtiene los productos almacenados en los espacios de Farma Central y los
   elimina vía API.
2. Trunca las tablas locales en PostgreSQL.
"""

import asyncio

from sqlalchemy import text

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal

ALL_SKUS = [
    # Insumos primarios
    "API-AMOXI-500",
    "API-IBUPRO-400",
    "API-SALBUTA",
    "EXC-LACTOSA-DC",
    "LAM-BLISTER-PVC",
    "EXC-JARABE-BASE",
    "FRA-VIDRIO-120",
    # Intermedios
    "BLI-AMOXI-500",
    "BLI-IBUPRO-400",
    "FRA-SALBUTA-120",
    # Kits
    "KIT-RESP-ADULTO",
]


async def reset_farma_central_sandbox(client: FarmaCentralClient) -> None:
    print("1. Limpiando productos en bodegas de Farma Central...")
    try:
        spaces = await client.get_spaces()
        deleted_count = 0

        for space in spaces:
            space_id = space["_id"]
            for sku in ALL_SKUS:
                try:
                    # Consultar productos de ese SKU en la bodega
                    products = await client.get_space_products(space_id, sku)
                    for prod in products:
                        prod_id = prod["_id"]
                        try:
                            await client.delete(f"/products/{prod_id}")
                            deleted_count += 1
                        except Exception:
                            pass
                except Exception:
                    pass

        print(f"   ✅ {deleted_count} productos eliminados de Farma Central.")
    except Exception as e:
        print(f"   ⚠️ Error al limpiar bodegas de Farma Central: {e}")


def reset_local_database() -> None:
    print("2. Limpiando tablas en la base de datos local (PostgreSQL)...")
    db = SessionLocal()
    try:
        db.execute(
            text(
                "TRUNCATE units, lots, production_runs, production_inputs, "
                "custody_events CASCADE;"
            )
        )
        db.commit()
        print("   ✅ Base de datos local truncada con éxito.")
    except Exception as e:
        db.rollback()
        print(f"   ⚠️ Error al limpiar la base de datos local: {e}")
    finally:
        db.close()


async def main():
    print("=== INICIANDO LIMPIEZA COMPLETA DE INVENTARIO ===")
    async with FarmaCentralClient() as client:
        await reset_farma_central_sandbox(client)

    reset_local_database()
    print("=== LIMPIEZA FINALIZADA ===")


if __name__ == "__main__":
    asyncio.run(main())
