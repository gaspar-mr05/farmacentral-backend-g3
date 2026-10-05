"""Move every available unit out of packaging and into the external buffer."""

import asyncio

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient, JSONResponse
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.db.session import SessionLocal
from app.models import Location, ProductionRun, Unit
from app.schemas.farma_central import (
    FarmaCentralSpace,
)
from app.services.inventory.movements import ProductMovementService
from app.services.inventory.sync import InventorySyncService


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

    spaces = _parse_list(await client.get_spaces(), FarmaCentralSpace, "spaces")
    packaging = _single_space(spaces, role="packaging")
    buffer = _single_space(spaces, role="buffer")
    movement_service = ProductMovementService(client, session)
    moved = 0

    units = list(
        session.scalars(
            select(Unit)
            .join(Location, Unit.current_location_id == Location.id)
            .where(
                Location.code == packaging.external_id,
                Unit.status == "available",
            )
            .order_by(Unit.id)
        )
    )
    for unit in units:
        result = await movement_service.move(
            product_id=unit.external_unit_id,
            destination_store=buffer.external_id,
        )
        moved += int(result.moved)

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


async def main() -> None:
    with SessionLocal() as session:
        async with FarmaCentralClient() as client:
            moved = await empty_packaging(client, session)
    print(f"Packaging emptied successfully: {moved} units moved to the buffer")


if __name__ == "__main__":
    asyncio.run(main())
