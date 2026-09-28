from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import FarmaCentralHTTPError
from app.models import Location, Lot, Product, Unit
from app.schemas.movements import ProductMovementResponse
from app.services.inventory.movements import ProductMovementService


class NoRefrigeratedLocationError(Exception):
    """Raised when exposed units exist but no refrigerated location is available."""


class ColdChainService:
    def __init__(
        self,
        client: FarmaCentralClient,
        session: Session,
    ) -> None:
        self._session = session
        self._movement_service = ProductMovementService(client, session)

    async def relocate_exposed_units(self) -> list[ProductMovementResponse]:
        exposed_units = self._find_exposed_units()
        if not exposed_units:
            return []

        destinations = self._find_refrigerated_locations()
        if not destinations:
            raise NoRefrigeratedLocationError("No refrigerated location is available")

        results = []
        for unit in exposed_units:
            results.append(
                await self._move_to_available_destination(
                    unit.external_unit_id,
                    destinations,
                )
            )

        return results

    async def _move_to_available_destination(
        self,
        product_id: str,
        destinations: list[Location],
    ) -> ProductMovementResponse:
        last_conflict: FarmaCentralHTTPError | None = None
        for destination in destinations:
            try:
                return await self._movement_service.move(
                    product_id=product_id,
                    destination_store=destination.code,
                )
            except FarmaCentralHTTPError as exc:
                if exc.status_code != 409:
                    raise
                last_conflict = exc

        if last_conflict is None:  # pragma: no cover - guarded by the caller
            raise NoRefrigeratedLocationError("No refrigerated location is available")
        raise last_conflict

    def _find_exposed_units(self) -> list[Unit]:
        statement = (
            select(Unit)
            .join(Lot, Unit.lot_id == Lot.id)
            .join(Product, Lot.product_id == Product.id)
            .join(Location, Unit.current_location_id == Location.id)
            .where(
                Product.requires_refrigeration.is_(True),
                Location.is_refrigerated.is_(False),
                Unit.status == "available",
            )
            .order_by(Unit.external_unit_id)
        )
        return list(self._session.scalars(statement))

    def _find_refrigerated_locations(self) -> list[Location]:
        statement = (
            select(Location)
            .where(Location.is_refrigerated.is_(True))
            .order_by(Location.code)
        )
        return list(self._session.scalars(statement))
