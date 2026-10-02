"""Verify that every sellable kit has enough completed production runs."""

import argparse
import asyncio

from sqlalchemy import func, select

from app.clients.farma_central import FarmaCentralClient
from app.db.session import SessionLocal
from app.models import ProductionRun
from app.schemas.farma_central import FarmaCentralProduct


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum", type=int, default=30)
    return parser.parse_args()


async def main() -> None:
    args = _arguments()
    async with FarmaCentralClient() as client:
        payload = await client.get_available_products()
    products = [FarmaCentralProduct.model_validate(item) for item in payload]
    kit_skus = sorted(product.sku for product in products if product.sellable)

    failures = []
    with SessionLocal() as session:
        for sku in kit_skus:
            quantity = session.scalar(
                select(
                    func.coalesce(func.sum(ProductionRun.expected_quantity), 0)
                ).where(
                    ProductionRun.expected_sku == sku,
                    ProductionRun.completed_at.is_not(None),
                    ProductionRun.output_lot_id.is_not(None),
                )
            )
            print(f"{sku}: {quantity}/{args.minimum}")
            if quantity < args.minimum:
                failures.append(sku)

    if failures:
        raise SystemExit(
            "Production requirement is incomplete for: " + ", ".join(failures)
        )


if __name__ == "__main__":
    asyncio.run(main())
