from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_farma_central_client, get_market_price_client
from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import (
    FarmaCentralConnectionError,
    FarmaCentralError,
    FarmaCentralTimeoutError,
)
from app.clients.market_prices import MarketPriceClient
from app.db.session import get_session
from app.schemas.orders import OrderCreate, OrderResponse
from app.services.catalog import CatalogPriceUnavailableError
from app.services.order_dispatch import InvalidDispatchStateError, OrderDispatchService
from app.services.order_fulfillment import (
    InsufficientFulfillmentStockError,
    InvalidFulfillmentStateError,
    OrderFulfillmentService,
    OrderNotPaidError,
)
from app.services.orders import (
    InsufficientStockError,
    OrderNotFoundError,
    OrderService,
    ProductNotAvailableError,
    get_order,
)

router = APIRouter(tags=["orders"])


@router.post(
    "/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_order(
    request: OrderCreate,
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[MarketPriceClient, Depends(get_market_price_client)],
) -> OrderResponse:
    try:
        return OrderResponse.model_validate(
            await OrderService(client, session).create(request)
        )
    except ProductNotAvailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InsufficientStockError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (FarmaCentralError, CatalogPriceUnavailableError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Current market prices could not be obtained",
        ) from exc


@router.get("/orders/{order_id}", response_model=OrderResponse)
def read_order(
    order_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> OrderResponse:
    try:
        return OrderResponse.model_validate(get_order(session, order_id))
    except OrderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/orders/{order_id}/fulfillment", response_model=OrderResponse)
def fulfill_order(
    order_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> OrderResponse:
    try:
        order = OrderFulfillmentService(session).fulfill(order_id)
        return OrderResponse.model_validate(order)
    except OrderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except (
        InsufficientFulfillmentStockError,
        InvalidFulfillmentStateError,
        OrderNotPaidError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc


@router.post("/orders/{order_id}/dispatch", response_model=OrderResponse)
async def dispatch_order(
    order_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[FarmaCentralClient, Depends(get_farma_central_client)],
) -> OrderResponse:
    try:
        order = await OrderDispatchService(client, session).dispatch(order_id)
        return OrderResponse.model_validate(order)
    except OrderNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidDispatchStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FarmaCentralConnectionError, FarmaCentralTimeoutError) as exc:
        raise HTTPException(
            status_code=503, detail="Farma Central is unavailable"
        ) from exc
    except FarmaCentralError as exc:
        raise HTTPException(
            status_code=502, detail="Dispatch could not be confirmed"
        ) from exc
