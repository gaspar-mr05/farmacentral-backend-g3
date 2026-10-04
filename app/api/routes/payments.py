from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_checkout_client
from app.clients.checkout import CheckoutClient
from app.clients.checkout_exceptions import (
    CheckoutConnectionError,
    CheckoutError,
    CheckoutTimeoutError,
)
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.schemas.payments import PaymentResponse, PaymentStartResponse
from app.services.orders import OrderNotFoundError, get_order
from app.services.payments import (
    OrderAlreadyPaidError,
    PaymentAlreadyPendingError,
    PaymentNotFoundError,
    PaymentService,
)

router = APIRouter(tags=["payments"])


@router.post(
    "/orders/{order_id}/payments",
    response_model=PaymentStartResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_payment(
    order_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[CheckoutClient, Depends(get_checkout_client)],
) -> PaymentStartResponse:
    try:
        order = get_order(session, order_id)
        return await PaymentService(client, session).start(order)
    except OrderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except (OrderAlreadyPaidError, PaymentAlreadyPendingError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except (CheckoutConnectionError, CheckoutTimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkout is unavailable",
        ) from exc
    except CheckoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Checkout could not initialize the payment",
        ) from exc


@router.get(
    "/payments/{payment_id}/return/{result}",
    response_model=PaymentResponse,
)
async def confirm_payment(
    payment_id: UUID,
    result: Literal["success", "error", "cancelled"],
    session: Annotated[Session, Depends(get_session)],
    client: Annotated[CheckoutClient, Depends(get_checkout_client)],
) -> PaymentResponse | RedirectResponse:
    _ = result
    try:
        payment = await PaymentService(client, session).confirm(payment_id)
        response = PaymentResponse.model_validate(payment)
    except PaymentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except (CheckoutConnectionError, CheckoutTimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkout is unavailable",
        ) from exc
    except CheckoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment status could not be verified",
        ) from exc

    settings: Settings = get_settings()
    if settings.frontend_public_url is None:
        return response
    query = urlencode(
        {
            "payment_id": response.id,
            "order_id": response.order_id,
            "status": response.status,
        }
    )
    target = f"{str(settings.frontend_public_url).rstrip('/')}/payment-result?{query}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
