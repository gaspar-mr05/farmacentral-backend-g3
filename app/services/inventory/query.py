from sqlalchemy.orm import Session

from app.db.products import list_products
from app.queries.inventory import list_available_inventory
from app.schemas.inventory_api import InventoryItemResponse
from app.schemas.locations import LocationResponse
from app.schemas.lots import LotResponse
from app.schemas.products import ProductResponse
from app.schemas.units import UnitResponse


class InventoryQueryService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_products(self) -> list[ProductResponse]:
        products = list_products(self._session)

        return [
            ProductResponse(
                sku=product.sku,
                name=product.name,
                category=product.category,
                batch_size=product.batch_size,
                requires_refrigeration=product.requires_refrigeration,
            )
            for product in products
        ]

    def get_available_inventory(
        self,
        *,
        sku: str | None = None,
        location_code: str | None = None,
    ) -> list[InventoryItemResponse]:
        units = list_available_inventory(
            self._session,
            sku=sku,
            location_code=location_code,
        )

        return [self._to_inventory_item(unit) for unit in units]

    @staticmethod
    def _to_inventory_item(unit) -> InventoryItemResponse:
        product = unit.lot.product
        lot = unit.lot
        location = unit.current_location

        return InventoryItemResponse(
            unit=UnitResponse(
                external_unit_id=unit.external_unit_id,
                status=unit.status,
            ),
            product=ProductResponse(
                sku=product.sku,
                name=product.name,
                category=product.category,
                batch_size=product.batch_size,
                requires_refrigeration=product.requires_refrigeration,
            ),
            lot=LotResponse(
                external_lot_id=lot.external_lot_id,
                expires_at=lot.expires_at,
            ),
            location=LocationResponse(
                code=location.code,
                name=location.name,
                is_refrigerated=location.is_refrigerated,
            ),
        )
