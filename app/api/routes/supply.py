from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_farma_central_client
from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import (
    FarmaCentralConnectionError,
    FarmaCentralError,
    FarmaCentralHTTPError,
    FarmaCentralTimeoutError,
)
from app.db.session import get_session
from app.schemas.supply import SupplyRequest, SupplyResponse
from app.services.supply.requests import (
    ChallengeExpiredError,
    InvalidSupplyRequestError,
    SupplyProductNotFoundError,
    SupplyRequestService,
)

router = APIRouter(tags=["supply"])


@router.post(
    "/supply-requests",
    response_model=SupplyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def request_supply(
    request: SupplyRequest,
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[
        FarmaCentralClient,
        Depends(get_farma_central_client),
    ],
) -> SupplyResponse:
    service = SupplyRequestService(client, session)
    try:
        return await service.request(sku=request.sku, quantity=request.quantity)
    except SupplyProductNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidSupplyRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except ChallengeExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except (FarmaCentralConnectionError, FarmaCentralTimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Farma Central is unavailable",
        ) from exc
    except FarmaCentralHTTPError as exc:
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        elif exc.status_code == status.HTTP_409_CONFLICT:
            status_code = status.HTTP_409_CONFLICT
        else:
            status_code = status.HTTP_502_BAD_GATEWAY
        raise HTTPException(
            status_code=status_code,
            detail="Farma Central rejected the supply request",
        ) from exc
    except FarmaCentralError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Farma Central returned an invalid response",
        ) from exc
