from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.db.locations import get_location_by_code
from app.db.units import get_unit_by_external_id
from app.schemas.movements import ProductMovementResponse
from app.services.custody.events import record_move


class ProductMovementError(Exception):
    """Base error for a rejected local movement."""


class UnitNotFoundError(ProductMovementError):
    def __init__(self, product_id: str) -> None:
        super().__init__(f"Unit {product_id} was not found")


class DestinationNotFoundError(ProductMovementError):
    def __init__(self, store_id: str) -> None:
        super().__init__(f"Destination store {store_id} was not found")


class UnitUnavailableError(ProductMovementError):
    def __init__(self, product_id: str) -> None:
        super().__init__(f"Unit {product_id} is not available")


class ProductMovementService:
    def __init__(
        self,
        client: FarmaCentralClient,
        session: Session,
    ) -> None:
        self._client = client
        self._session = session

    async def move(
        self,
        *,
        product_id: str,
        destination_store: str,
    ) -> ProductMovementResponse:
        unit = get_unit_by_external_id(
            self._session,
            product_id,
            for_update=True,
        )
        if unit is None:
            raise UnitNotFoundError(product_id)

        destination = get_location_by_code(
            self._session,
            destination_store,
        )
        if destination is None:
            raise DestinationNotFoundError(destination_store)

        if unit.status != "available":
            raise UnitUnavailableError(product_id)

        origin = unit.current_location
        origin_code = origin.code

        if origin.id == destination.id:
            return ProductMovementResponse(
                product_id=product_id,
                from_store=origin_code,
                to_store=destination.code,
                moved=False,
            )

        try:
            await self._client.move_product(
                product_id=product_id,
                store_id=destination.code,
            )

            record_move(
                self._session,
                unit=unit,
                to_location_id=destination.id,
            )

            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        return ProductMovementResponse(
            product_id=product_id,
            from_store=origin_code,
            to_store=destination.code,
            moved=True,
        )
