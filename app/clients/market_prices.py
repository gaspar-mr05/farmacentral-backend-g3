from typing import Any

import httpx

from app.clients.farma_central_exceptions import (
    FarmaCentralConnectionError,
    FarmaCentralHTTPError,
    FarmaCentralInvalidResponseError,
    FarmaCentralTimeoutError,
)
from app.core.config import Settings, get_settings


class MarketPriceClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        config = settings or get_settings()
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            base_url=f"{str(config.farma_central_base_url).rstrip('/')}/",
            headers={"Accept": "application/json"},
            timeout=config.farma_central_timeout_seconds,
        )

    async def __aenter__(self) -> "MarketPriceClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def get_current_prices(self) -> list[dict[str, Any]]:
        try:
            response = await self._http_client.get("market/prices")
        except httpx.TimeoutException as exc:
            raise FarmaCentralTimeoutError(
                "The market price service did not respond within the configured timeout"
            ) from exc
        except httpx.ConnectError as exc:
            raise FarmaCentralConnectionError(
                "Could not connect to the market price service"
            ) from exc

        if response.is_error:
            raise FarmaCentralHTTPError(response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise FarmaCentralInvalidResponseError(
                "The market price service returned invalid JSON"
            ) from exc

        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise FarmaCentralInvalidResponseError(
                "The market price service returned an unexpected JSON value"
            )

        return payload
