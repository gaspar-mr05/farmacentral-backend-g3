import asyncio
import logging
from contextlib import suppress
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from app.clients.purchase_orders_exceptions import (
    PurchaseOrdersAuthenticationError,
    PurchaseOrdersConnectionError,
    PurchaseOrdersHTTPError,
    PurchaseOrdersInvalidResponseError,
    PurchaseOrdersTimeoutError,
)
from app.core.config import Settings, get_settings
from app.schemas.purchase_orders import (
    PurchaseOrder,
    PurchaseOrderDecision,
    PurchaseOrderId,
    PurchaseOrderStateUpdate,
)

logger = logging.getLogger(__name__)
ORDER_ID_ADAPTER = TypeAdapter(PurchaseOrderId)


class PurchaseOrdersClient:
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
        self._auth_lock = asyncio.Lock()
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            base_url=f"{config.effective_purchase_orders_base_url.rstrip('/')}/",
            headers={"Accept": "application/json"},
            timeout=config.purchase_orders_timeout_seconds,
        )

    async def __aenter__(self) -> "PurchaseOrdersClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def get_order(self, order_id: str) -> PurchaseOrder:
        order_id = ORDER_ID_ADAPTER.validate_python(order_id).lower()
        response = await self._request("GET", f"ordenes/{order_id}")
        self._expect_status(response, 200)
        try:
            order = PurchaseOrder.model_validate(self._decode_json(response))
        except ValidationError as exc:
            logger.warning("Invalid purchase order response: order_id=%s", order_id)
            raise PurchaseOrdersInvalidResponseError(
                "Purchase orders service returned invalid order data"
            ) from exc
        if order.id.lower() != order_id:
            raise PurchaseOrdersInvalidResponseError(
                "Purchase orders service returned a different order ID"
            )
        logger.info(
            "Purchase order fetched: order_id=%s state=%s", order.id, order.state
        )
        return order

    async def update_order_state(
        self, order_id: str, state: PurchaseOrderDecision
    ) -> None:
        order_id = ORDER_ID_ADAPTER.validate_python(order_id).lower()
        request = PurchaseOrderStateUpdate(estado=state)
        response = await self._request(
            "POST", f"ordenes/{order_id}/estado", json=request.model_dump(by_alias=True)
        )
        self._expect_status(response, 204)
        logger.info(
            "Purchase order state updated: order_id=%s state=%s", order_id, state
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._get_token()
        response = await self._send(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if response.status_code == 401:
            token = await self._refresh_token(token)
            response = await self._send(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        self._raise_for_status(response)
        return response

    async def _get_token(self) -> str:
        async with self._auth_lock:
            if self._token is None:
                self._token = await self._authenticate()
            return self._token

    async def _refresh_token(self, rejected_token: str) -> str:
        async with self._auth_lock:
            if self._token == rejected_token:
                self._token = await self._authenticate()
            return self._token

    async def _authenticate(self) -> str:
        response = await self._send(
            "POST", "autenticar", json={"group": self._group, "secret": self._secret}
        )
        self._raise_for_status(response, authenticating=True)
        self._expect_status(response, 200)
        payload = self._decode_json(response)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise PurchaseOrdersInvalidResponseError(
                "Purchase order authentication did not return a token"
            )
        return token

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http_client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("Purchase order request timed out: %s %s", method, path)
            raise PurchaseOrdersTimeoutError(
                "Purchase order request timed out"
            ) from exc
        except httpx.RequestError as exc:
            logger.warning("Purchase order connection failed: %s %s", method, path)
            raise PurchaseOrdersConnectionError(
                "Could not communicate with the purchase orders service"
            ) from exc

    @staticmethod
    def _raise_for_status(
        response: httpx.Response, *, authenticating: bool = False
    ) -> None:
        if not response.is_error:
            return
        logger.warning(
            "Purchase order request failed: %s %s HTTP %s",
            response.request.method,
            response.request.url.path,
            response.status_code,
        )
        if response.status_code == 401 or (
            authenticating and response.status_code in (400, 403)
        ):
            raise PurchaseOrdersAuthenticationError(response.status_code)
        retry_after = None
        if response.status_code == 429:
            with suppress(KeyError, ValueError):
                retry_after = max(float(response.headers["Retry-After"]), 0)
        raise PurchaseOrdersHTTPError(
            response.status_code, retry_after_seconds=retry_after
        )

    @staticmethod
    def _expect_status(response: httpx.Response, expected: int) -> None:
        if response.status_code != expected:
            raise PurchaseOrdersInvalidResponseError(
                f"Purchase orders returned unexpected HTTP {response.status_code}"
            )

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise PurchaseOrdersInvalidResponseError(
                "Purchase orders service returned invalid JSON"
            ) from exc
