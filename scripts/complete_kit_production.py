"""Produce the required quantity of every sellable kit in Farma Central."""

import argparse
import asyncio
import logging
from urllib.parse import urlsplit

from app.clients.farma_central import FarmaCentralClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.production.kits import KitProductionService
from scripts.empty_packaging import empty_packaging


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quantity", type=int, default=30)
    parser.add_argument(
        "--sku",
        action="append",
        help="Produce only this kit SKU; repeat for multiple kits",
    )
    parser.add_argument(
        "--raw-material-source",
        choices=("supply", "sandbox"),
        default="supply",
    )
    parser.add_argument("--allow-production", action="store_true")
    return parser.parse_args()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _arguments()
    if args.quantity <= 0:
        raise SystemExit("--quantity must be positive")

    settings = get_settings()
    hostname = urlsplit(str(settings.farma_central_base_url)).hostname or ""
    is_production = hostname.startswith("prod.")
    if is_production and not args.allow_production:
        raise SystemExit(
            "Refusing to mutate Farma Central production without --allow-production"
        )
    if is_production and args.raw_material_source == "sandbox":
        raise SystemExit("Sandbox provisioning is not allowed in production")

    with SessionLocal() as session:
        async with FarmaCentralClient() as client:
            service = KitProductionService(
                client,
                session,
                raw_material_source=args.raw_material_source,
            )
            if args.sku:
                runs = await service.produce_kits(
                    skus=args.sku,
                    quantity_per_sku=args.quantity,
                )
            else:
                runs = await service.produce_all_kits(quantity_per_sku=args.quantity)
            moved = await empty_packaging(client, session)

    for sku, sku_runs in runs.items():
        produced = sum(run.expected_quantity for run in sku_runs)
        print(f"{sku}: {produced} new units across {len(sku_runs)} runs")
    print(f"Moved {moved} units from packaging to the buffer")


if __name__ == "__main__":
    asyncio.run(main())
