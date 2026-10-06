from math import ceil

from fastapi import HTTPException, status

from app.clients.farma_central_exceptions import (
    FarmaCentralConnectionError,
    FarmaCentralError,
    FarmaCentralHTTPError,
    FarmaCentralTimeoutError,
)


def market_price_http_exception(exc: FarmaCentralError) -> HTTPException:
    unavailable = isinstance(
        exc,
        (FarmaCentralConnectionError, FarmaCentralTimeoutError),
    ) or (
        isinstance(exc, FarmaCentralHTTPError)
        and (exc.status_code == 429 or exc.status_code >= 500)
    )
    if not unavailable:
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Current market prices could not be obtained",
        )

    headers = None
    if isinstance(exc, FarmaCentralHTTPError) and exc.retry_after_seconds is not None:
        headers = {"Retry-After": str(ceil(exc.retry_after_seconds))}
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="The market price service is unavailable",
        headers=headers,
    )
