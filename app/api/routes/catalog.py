from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_market_price_client
from app.clients.farma_central_exceptions import (
    FarmaCentralConnectionError,
    FarmaCentralError,
    FarmaCentralTimeoutError,
)
from app.clients.market_prices import MarketPriceClient
from app.db.session import get_session
from app.schemas.catalog import CatalogItemResponse
from app.services.catalog import CatalogPriceUnavailableError, CatalogService

router = APIRouter(tags=["catalog"])


@router.get(
    "/catalog",
    response_model=list[CatalogItemResponse],
)
async def get_catalog(
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[
        MarketPriceClient,
        Depends(get_market_price_client),
    ],
) -> list[CatalogItemResponse]:
    try:
        return await CatalogService(client, session).list_items()
    except (FarmaCentralConnectionError, FarmaCentralTimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The market price service is unavailable",
        ) from exc
    except (FarmaCentralError, CatalogPriceUnavailableError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Current market prices could not be obtained",
        ) from exc
