import asyncio
import logging
import re
from typing import Any

import httpx

from app.clients.farma_central_exceptions import (
    FarmaCentralConnectionError,
    FarmaCentralHTTPError,
    FarmaCentralInvalidResponseError,
    FarmaCentralTimeoutError,
)
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class MarketPriceClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        config = settings or get_settings()
        self._owns_http_client = http_client is None
        self._cache_ttl_seconds = config.market_price_cache_ttl_seconds
        self._max_retry_wait_seconds = config.market_price_max_retry_wait_seconds
        self._cached_prices: list[dict[str, Any]] | None = None
        self._cache_expires_at = 0.0
        self._refresh_lock = asyncio.Lock()
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

    async def get_current_prices(
        self, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        if use_cache:
            cached = self._get_cached_prices()
            if cached is not None:
                return cached

        async with self._refresh_lock:
            if use_cache:
                cached = self._get_cached_prices()
                if cached is not None:
                    return cached

            prices = await self._fetch_current_prices()
            self._cached_prices = prices
            self._cache_expires_at = (
                asyncio.get_running_loop().time() + self._cache_ttl_seconds
            )
            return _copy_prices(prices)

    async def _fetch_current_prices(self) -> list[dict[str, Any]]:
        response = await self._request_prices()
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response)
            if retry_after is not None and retry_after <= self._max_retry_wait_seconds:
                await asyncio.sleep(retry_after)
                response = await self._request_prices()

        if response.is_error:
            retry_after = _retry_after_seconds(response)
            logger.warning(
                "Market price service returned HTTP %s (retry_after=%s)",
                response.status_code,
                retry_after,
            )
            raise FarmaCentralHTTPError(
                response.status_code,
                retry_after_seconds=retry_after,
            )

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

    async def _request_prices(self) -> httpx.Response:
        try:
            return await self._http_client.get("market/prices")
        except httpx.TimeoutException as exc:
            raise FarmaCentralTimeoutError(
                "The market price service did not respond within the configured timeout"
            ) from exc
        except httpx.ConnectError as exc:
            raise FarmaCentralConnectionError(
                "Could not connect to the market price service"
            ) from exc

    def _get_cached_prices(self) -> list[dict[str, Any]] | None:
        if (
            self._cached_prices is None
            or asyncio.get_running_loop().time() >= self._cache_expires_at
        ):
            return None
        return _copy_prices(self._cached_prices)


def _copy_prices(prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [price.copy() for price in prices]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(float(retry_after), 0)
        except ValueError:
            pass

    rate_limit = response.headers.get("RateLimit")
    if rate_limit is None:
        return None
    match = re.search(r"(?:^|[,;]\s*)reset=(\d+(?:\.\d+)?)", rate_limit)
    return float(match.group(1)) if match else None
