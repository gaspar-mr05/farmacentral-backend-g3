from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas.inventory_api import InventoryItemResponse
from app.services.inventory_query import InventoryQueryService

router = APIRouter(tags=["inventory"])


@router.get(
    "/inventory/available",
    response_model=list[InventoryItemResponse],
)
def get_available_inventory(
    session: Annotated[Session, Depends(get_session)],
    sku: Annotated[str | None, Query(min_length=1)] = None,
    location_code: Annotated[str | None, Query(min_length=1)] = None,
) -> list[InventoryItemResponse]:
    return InventoryQueryService(session).get_available_inventory(
        sku=sku,
        location_code=location_code,
    )
