"""Move every available unit out of packaging and into the external buffer.

Run this only after production has finished. The product catalog is discovered
from Farma Central so new intermediate products and kits do not require changes
to this script.
"""

import asyncio

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient, JSONResponse
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.db.session import SessionLocal
from app.models import ProductionRun
from app.schemas.farma_central import (
    FarmaCentralProduct,
    FarmaCentralSpace,
    FarmaCentralUnit,
)
from app.services.inventory.movements import ProductMovementService
from app.services.inventory.sync import InventorySyncService

PAGE_SIZE = 200


class PendingProductionRunsError(RuntimeError):
    """Raised when clearing packaging could interfere with active production."""


async def empty_packaging(
    client: FarmaCentralClient,
    session: Session,
) -> int:
    if session.scalar(
        select(ProductionRun.id).where(ProductionRun.completed_at.is_(None)).limit(1)
    ):
        raise PendingProductionRunsError(
            "Cannot empty packaging while production runs are pending"
        )

    await InventorySyncService(client, session).synchronize()

    products = _parse_list(
        await client.get_available_products(), FarmaCentralProduct, "products"
    )
    spaces = _parse_list(await client.get_spaces(), FarmaCentralSpace, "spaces")
    packaging = _single_space(spaces, role="packaging")
    buffer = _single_space(spaces, role="buffer")
    movement_service = ProductMovementService(client, session)
    moved = 0

    for product in sorted(products, key=lambda item: item.sku):
        while True:
            units = _parse_list(
                await client.get_space_products(
                    packaging.external_id,
                    product.sku,
                    limit=PAGE_SIZE,
                ),
                FarmaCentralUnit,
                f"packaging units for {product.sku}",
            )
            _validate_units(units, sku=product.sku, store_id=packaging.external_id)
            if not units:
                break

            moved_in_page = 0
            for unit in units:
                result = await movement_service.move(
                    product_id=unit.external_id,
                    destination_store=buffer.external_id,
                )
                moved_in_page += int(result.moved)

            if moved_in_page == 0:
                raise RuntimeError(
                    f"Farma Central still reports {product.sku} in packaging, "
                    "but no unit was moved"
                )
            moved += moved_in_page

    await InventorySyncService(client, session).synchronize()
    return moved


def _parse_list[Schema](
    payload: JSONResponse,
    schema: type[Schema],
    resource: str,
) -> list[Schema]:
    if not isinstance(payload, list):
        raise FarmaCentralInvalidResponseError(f"Invalid {resource}: expected a list")
    try:
        return [schema.model_validate(item) for item in payload]
    except (AttributeError, ValidationError) as exc:
        raise FarmaCentralInvalidResponseError(f"Invalid {resource}") from exc


def _single_space(
    spaces: list[FarmaCentralSpace],
    *,
    role: str,
) -> FarmaCentralSpace:
    matches = [space for space in spaces if getattr(space, role)]
    if len(matches) != 1:
        raise FarmaCentralInvalidResponseError(
            f"Expected exactly one {role} space, found {len(matches)}"
        )
    return matches[0]


def _validate_units(
    units: list[FarmaCentralUnit],
    *,
    sku: str,
    store_id: str,
) -> None:
    if any(unit.sku != sku or unit.store_id != store_id for unit in units):
        raise FarmaCentralInvalidResponseError(
            f"Farma Central returned inconsistent packaging units for {sku}"
        )


async def main() -> None:
    with SessionLocal() as session:
        async with FarmaCentralClient() as client:
            moved = await empty_packaging(client, session)
    print(f"Packaging emptied successfully: {moved} units moved to the buffer")


if __name__ == "__main__":
    asyncio.run(main())
