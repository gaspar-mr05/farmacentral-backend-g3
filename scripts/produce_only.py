import asyncio
import sys

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce

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
    if len(sys.argv) < 3:
        print("Uso: python produce_only.py <SKU> <CANTIDAD>")
        return

    target_sku = sys.argv[1]
    target_qty = int(sys.argv[2])

    session = SessionLocal()
    async with FarmaCentralClient() as client:
        spaces = await client.get_spaces()
        packaging_space = next(s for s in spaces if s.get("packaging") is True)
        other_spaces = [s for s in spaces if s["_id"] != packaging_space["_id"]]

        print("1. Vaciando bodega de empaque (para liberar capacidad)...")
        for sku in ALL_SKUS:
            while True:
                try:
                    in_packaging = await client.get_space_products(
                        packaging_space["_id"], sku=sku
                    )
                    if not in_packaging:
                        break
                    moved = 0
                    for p in in_packaging:
                        try:
                            await client.move_product(p["_id"], other_spaces[0]["_id"])
                            moved += 1
                        except Exception:
                            pass
                    if moved == 0:
                        break  # Evita atraparse si algo falla
                except Exception:
                    break

        print("2. Calculando receta y buscando insumos (sorteando límite de 30)...")
        catalog = await client.get_available_products()
        product_info = next((p for p in catalog if p["sku"] == target_sku), None)

        if product_info and "components" in product_info:
            for comp in product_info["components"]:
                needed_qty = comp["req"] * target_qty
                gathered = 0

                for space in other_spaces:
                    while gathered < needed_qty:
                        avail = await client.get_space_products(
                            space["_id"], sku=comp["sku"]
                        )
                        if not avail:
                            break  # Ya no hay más de este SKU en la página

                        moved_this_round = 0
                        for p in avail:
                            if gathered >= needed_qty:
                                break
                            try:
                                await client.move_product(
                                    p["_id"], packaging_space["_id"]
                                )
                                gathered += 1
                                moved_this_round += 1
                            except Exception:
                                pass

                        if moved_this_round == 0:
                            break  # Si no movió nada, salir del bucle

                print(
                    f"   -> Se movieron {gathered}/{needed_qty} de "
                    f"{comp['sku']} a empaque."
                )
                if gathered < needed_qty:
                    print(
                        "   ⚠️ ADVERTENCIA: Faltan "
                        f"{needed_qty - gathered} unidades de {comp['sku']}"
                    )

        print(f"3. Fabricando {target_qty} unidades de {target_sku}...")
        try:
            run, supply = await produce(
                session, client=client, sku=target_sku, quantity=target_qty
            )
            print(f"✅ Producción exitosa. Run ID: {run.id}")
        except Exception as e:
            print(f"❌ Error en producción: {e}")


if __name__ == "__main__":
    asyncio.run(main())
