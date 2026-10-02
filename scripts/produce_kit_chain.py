"""Produce la cadena completa: insumos -> intermedios -> kit.
Maneja de forma automática el movimiento de inventarios, la paginación
y el límite de capacidad de la bodega de empaque.
"""

import asyncio
from datetime import UTC, datetime

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce
from scripts.farma_central_common import get_buffer_and_packaging

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


async def prepare_and_produce(
    session,
    client,
    target_sku,
    target_qty,
    buffer_space,
    packaging_space,
    other_spaces,
    catalog,
):
    # 1. Vaciar la bodega de empaque
    print(f"\n[1] Vaciando empaque para {target_sku}...")
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
                        await client.move_product(p["_id"], buffer_space["_id"])
                        moved += 1
                    except Exception:
                        pass
                if moved == 0:
                    break
            except Exception:
                break

    # 2. Calcular receta y mover insumos necesarios sorteando paginación
    print(f"[2] Buscando y moviendo insumos para {target_qty}x {target_sku}...")
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
                        break

                    moved_this_round = 0
                    for p in avail:
                        if gathered >= needed_qty:
                            break
                        try:
                            await client.move_product(p["_id"], packaging_space["_id"])
                            gathered += 1
                            moved_this_round += 1
                        except Exception:
                            pass

                    if moved_this_round == 0:
                        break

            print(f"    -> Se movieron {gathered}/{needed_qty} de {comp['sku']}.")
            if gathered < needed_qty:
                print(
                    "    ⚠️ ADVERTENCIA: Faltan "
                    f"{needed_qty - gathered} unidades de {comp['sku']}"
                )

    # 3. Fabricar
    print(f"[3] Fabricando {target_qty} unidades de {target_sku}...")
    run, supply = await produce(
        session, client=client, sku=target_sku, quantity=target_qty
    )
    print(f"✅ {target_sku} fabricado exitosamente. Run ID: {run.id}")
    return supply.available_at


async def main():
    session = SessionLocal()
    async with FarmaCentralClient() as client:
        buffer_space, packaging_space = await get_buffer_and_packaging(client)
        spaces = await client.get_spaces()
        other_spaces = [s for s in spaces if s["_id"] != packaging_space["_id"]]
        catalog = await client.get_available_products()

        # Batch sizes reales según Farma Central
        intermediate_targets = {
            "BLI-AMOXI-500": 3,
            "BLI-IBUPRO-400": 4,
            "FRA-SALBUTA-120": 3,
        }

        available_times = []

        print("=============================================")
        print(" FASE 1: PRODUCCIÓN DE INTERMEDIOS")
        print("=============================================")
        for sku, qty in intermediate_targets.items():
            try:
                avail_at = await prepare_and_produce(
                    session,
                    client,
                    sku,
                    qty,
                    buffer_space,
                    packaging_space,
                    other_spaces,
                    catalog,
                )
                available_times.append(avail_at)
            except Exception as e:
                print(f"❌ Error crítico fabricando {sku}: {e}")
                return

        print("\n=============================================")
        print(" FASE 2: ESPERA DE DISPONIBILIDAD")
        print("=============================================")
        if available_times:
            # Encontrar el tiempo máximo de espera
            valid_times = [t for t in available_times if t is not None]
            if valid_times:
                max_time = max(valid_times)
                # Asegurar timezone
                if max_time.tzinfo is None:
                    max_time = max_time.replace(tzinfo=UTC)

                now = datetime.now(UTC)
                if max_time > now:
                    wait_seconds = (
                        max_time - now
                    ).total_seconds() + 5  # 5s de margen extra
                    print(
                        f"⏳ Esperando {int(wait_seconds)} segundos para el "
                        "acondicionamiento..."
                    )
                    await asyncio.sleep(wait_seconds)
                else:
                    print("✅ Los intermedios ya están listos para usarse.")

        print("\n=============================================")
        print(" FASE 3: PRODUCCIÓN DEL KIT FINAL")
        print("=============================================")
        try:
            # Produce 1 Kit (consume 1 unidad de cada intermedio)
            await prepare_and_produce(
                session,
                client,
                "KIT-RESP-ADULTO",
                1,
                buffer_space,
                packaging_space,
                other_spaces,
                catalog,
            )
            print("\n🎉 ¡CADENA DE PRODUCCIÓN COMPLETADA CON ÉXITO!")
        except Exception as e:
            print(f"❌ Error crítico fabricando el Kit: {e}")


if __name__ == "__main__":
    asyncio.run(main())
