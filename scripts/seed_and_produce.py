"""Flujo completo de prueba: siembra insumos en sandbox, los mueve a
acondicionamiento, y dispara una producción real contra Farma Central dev.
Todo en una sola ejecución continua para minimizar riesgo de vencimiento."""

import asyncio

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.services.production.orchestration import produce
from scripts.farma_central_common import get_buffer_and_packaging

REQUIRED_MATERIALS = {
    "API-AMOXI-500": 36,
    "EXC-LACTOSA-DC": 24,
    "LAM-BLISTER-PVC": 3,
}


async def main():
    async with FarmaCentralClient() as client:
        _, packaging_space = await get_buffer_and_packaging(client)

        created_ids: dict[str, list[str]] = {sku: [] for sku in REQUIRED_MATERIALS}
        for sku, count in REQUIRED_MATERIALS.items():
            for _ in range(count):
                result = await client.post("/sandbox/products", {"sku": sku})
                created_ids[sku].append(result["productId"])
        print("Insumos sembrados.")

        for _sku, ids in created_ids.items():
            for product_id in ids:
                await client.move_product(product_id, packaging_space["_id"])
        print("Insumos movidos a acondicionamiento.")

        session = SessionLocal()
        run, supply = await produce(
            session,
            client=client,
            sku="BLI-AMOXI-500",
            quantity=3,
        )
        print("ProductionRun:", run.id, "requested_at:", run.requested_at)
        print("Supply response:", supply)


if __name__ == "__main__":
    asyncio.run(main())
