from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_farma_central_client
from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import FarmaCentralError
from app.db.session import get_session
from app.schemas.movements import ProductMovementRequest, ProductMovementResponse
from app.schemas.products import ProductResponse
from app.services.inventory.movements import (
    DestinationNotFoundError,
    ProductMovementService,
    UnitNotFoundError,
    UnitUnavailableError,
)
from app.services.inventory.query import InventoryQueryService

router = APIRouter(tags=["products"])


@router.get("/products", response_model=list[ProductResponse])
def get_products(
    session: Annotated[Session, Depends(get_session)],
) -> list[ProductResponse]:
    return InventoryQueryService(session).get_products()


@router.patch(
    "/products/{product_id}",
    response_model=ProductMovementResponse,
)
async def move_product(
    product_id: str,
    movement: ProductMovementRequest,
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[
        FarmaCentralClient,
        Depends(get_farma_central_client),
    ],
) -> ProductMovementResponse:
    service = ProductMovementService(client, session)

    try:
        return await service.move(
            product_id=product_id,
            destination_store=movement.store,
        )
    except (UnitNotFoundError, DestinationNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except UnitUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except FarmaCentralError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Farma Central rejected or could not complete the movement",
        ) from exc
