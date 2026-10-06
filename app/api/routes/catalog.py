from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_market_price_client
from app.api.errors import market_price_http_exception
from app.clients.farma_central_exceptions import FarmaCentralError
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
    except FarmaCentralError as exc:
        raise market_price_http_exception(exc) from exc
    except CatalogPriceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Current market prices could not be obtained",
        ) from exc
