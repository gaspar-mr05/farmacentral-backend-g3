from typing import Any

import httpx

from app.clients.checkout_exceptions import (
    CheckoutConnectionError,
    CheckoutHTTPError,
    CheckoutInvalidResponseError,
    CheckoutTimeoutError,
)
from app.core.config import Settings, get_settings


class CheckoutClient:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        config = settings or get_settings()
        self._group = config.farma_central_group
        self._secret = config.farma_central_api_secret.get_secret_value()
        self._token: str | None = None
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            base_url=f"{str(config.checkout_base_url).rstrip('/')}/",
            headers={"Accept": "application/json"},
            timeout=config.farma_central_timeout_seconds,
        )

    async def __aenter__(self) -> "CheckoutClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def initialize_payment(
        self,
        *,
        amount: int,
        back_urls: dict[str, str],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "payments/init",
            json={"amount": amount, "group": self._group, "backUrls": back_urls},
        )

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("GET", f"payments/{payment_id}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = await self._get_token()
        response = await self._send(
            method,
            path,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        if response.status_code in (401, 403):
            self._token = await self._authenticate()
            response = await self._send(
                method,
                path,
                headers={"Authorization": f"Bearer {self._token}"},
                **kwargs,
            )
        return self._decode(response)

    async def _get_token(self) -> str:
        if self._token is None:
            self._token = await self._authenticate()
        return self._token

    async def _authenticate(self) -> str:
        response = await self._send(
            "POST",
            "payments/auth",
            json={"group": self._group, "secret": self._secret},
        )
        payload = self._decode(response)
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise CheckoutInvalidResponseError(
                "Checkout authentication response did not include a token"
            )
        return token

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http_client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise CheckoutTimeoutError("Checkout request timed out") from exc
        except httpx.ConnectError as exc:
            raise CheckoutConnectionError("Could not connect to checkout") from exc

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        if response.is_error:
            raise CheckoutHTTPError(response.status_code)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CheckoutInvalidResponseError(
                "Checkout returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise CheckoutInvalidResponseError(
                "Checkout returned an unexpected JSON value"
            )
        return payload
