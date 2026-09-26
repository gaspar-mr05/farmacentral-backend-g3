import asyncio
from typing import Any

import httpx

from app.clients.farma_central_exceptions import (
    FarmaCentralAuthenticationError,
    FarmaCentralConnectionError,
    FarmaCentralHTTPError,
    FarmaCentralInvalidResponseError,
    FarmaCentralTimeoutError,
)
from app.core.config import Settings, get_settings

JSONResponse = dict[str, Any] | list[Any]


class FarmaCentralClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        config = settings or get_settings()
        self._group = config.farma_central_group
        self._api_secret = config.farma_central_api_secret.get_secret_value()
        self._token: str | None = None
        self._authentication_lock = asyncio.Lock()
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            base_url=f"{str(config.farma_central_base_url).rstrip('/')}/",
            headers={"Accept": "application/json"},
            timeout=config.farma_central_timeout_seconds,
        )

    async def __aenter__(self) -> "FarmaCentralClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def get_available_products(self) -> JSONResponse:
        return await self.get("/products/available")

    async def get_spaces(self) -> JSONResponse:
        return await self.get("/spaces")

    async def get_space_inventory(self, store_id: str) -> JSONResponse:
        return await self.get(f"/spaces/{store_id}/inventory")

    async def get_space_products(self, store_id: str, sku: str) -> JSONResponse:
        return await self.get(f"/spaces/{store_id}/products", params={"sku": sku})

    async def get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> JSONResponse:
        return await self._request("GET", path, params=params)

    async def post(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> JSONResponse:
        return await self._request("POST", path, json=payload)

    async def _request(self, method: str, path: str, **kwargs: Any) -> JSONResponse:
        token = await self._get_token()
        response = await self._send(
            method, path, headers=self._authorization_headers(token), **kwargs
        )
        if response.status_code in (401, 403):
            token = await self._refresh_token(token)
            response = await self._send(
                method, path, headers=self._authorization_headers(token), **kwargs
            )

        self._raise_for_status(response)
        return self._decode_json(response)

    async def _get_token(self) -> str:
        if self._token is None:
            async with self._authentication_lock:
                if self._token is None:
                    self._token = await self._authenticate()
        return self._token

    async def _refresh_token(self, rejected_token: str) -> str:
        async with self._authentication_lock:
            if self._token == rejected_token:
                self._token = await self._authenticate()
            return self._token

    async def _authenticate(self) -> str:
        response = await self._send(
            "POST",
            "/auth",
            json={"group": self._group, "secret": self._api_secret},
        )
        if response.status_code in (400, 401, 403):
            raise FarmaCentralAuthenticationError(response.status_code)

        self._raise_for_status(response)
        payload = self._decode_json(response)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise FarmaCentralInvalidResponseError(
                "Farma Central authentication response did not include a token"
            )
        return token

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http_client.request(method, path.lstrip("/"), **kwargs)
        except httpx.TimeoutException as exc:
            raise FarmaCentralTimeoutError(
                "Farma Central did not respond within the configured timeout"
            ) from exc
        except httpx.ConnectError as exc:
            raise FarmaCentralConnectionError(
                "Could not connect to Farma Central"
            ) from exc

    @staticmethod
    def _authorization_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise FarmaCentralAuthenticationError(response.status_code)
        if response.is_error:
            raise FarmaCentralHTTPError(response.status_code)

    @staticmethod
    def _decode_json(response: httpx.Response) -> JSONResponse:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FarmaCentralInvalidResponseError(
                "Farma Central returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict | list):
            raise FarmaCentralInvalidResponseError(
                "Farma Central returned an unexpected JSON value"
            )
        return payload
