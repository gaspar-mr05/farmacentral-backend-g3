from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.clients.checkout import CheckoutClient
from app.clients.farma_central import FarmaCentralClient
from app.clients.market_prices import MarketPriceClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.core.config import get_settings
from app.core.oidc import (
    GoogleOIDCVerifier,
    InvalidOIDCTokenError,
    OIDCVerificationUnavailableError,
)

oidc_bearer = HTTPBearer(auto_error=False)


def get_oidc_verifier(request: Request) -> GoogleOIDCVerifier:
    return request.app.state.oidc_verifier


def require_messaging_oidc(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(oidc_bearer)],
    verifier: Annotated[GoogleOIDCVerifier, Depends(get_oidc_verifier)],
) -> None:
    unauthorized = HTTPException(
        status_code=401,
        detail="Invalid webhook authentication",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    settings = get_settings()
    try:
        verifier.verify(
            credentials.credentials,
            audience=settings.effective_messaging_oidc_audience,
            service_account=settings.messaging_oidc_service_account,
        )
    except InvalidOIDCTokenError as exc:
        raise unauthorized from exc
    except OIDCVerificationUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="Webhook authentication is temporarily unavailable",
        ) from exc


async def get_farma_central_client() -> AsyncGenerator[FarmaCentralClient, None]:
    async with FarmaCentralClient() as client:
        yield client


async def get_market_price_client(
    request: Request,
) -> AsyncGenerator[MarketPriceClient, None]:
    yield request.app.state.market_price_client


async def get_checkout_client() -> AsyncGenerator[CheckoutClient, None]:
    async with CheckoutClient() as client:
        yield client


async def get_purchase_orders_client() -> AsyncGenerator[PurchaseOrdersClient, None]:
    async with PurchaseOrdersClient() as client:
        yield client
