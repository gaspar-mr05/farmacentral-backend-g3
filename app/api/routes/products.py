from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas.products import ProductResponse
from app.services.inventory_query import InventoryQueryService

router = APIRouter(tags=["products"])


@router.get("/products", response_model=list[ProductResponse])
def get_products(
    session: Annotated[Session, Depends(get_session)],
) -> list[ProductResponse]:
    return InventoryQueryService(session).get_products()
