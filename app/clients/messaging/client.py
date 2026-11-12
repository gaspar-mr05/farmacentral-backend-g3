import asyncio
import logging
import math
from contextlib import suppress
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.schemas.messaging import (
    MessagingDiscardRequest,
    MessagingInquiryRequest,
    MessagingOrderInterpretation,
    MessagingOrderRequest,
    MessagingResponse,
    MessagingSendRequest,
    MessagingWebhookPayload,
)

from .exceptions import (
    MessagingAlreadyRespondedError,
    MessagingAuthenticationError,
    MessagingConnectionError,
    MessagingHTTPError,
    MessagingInvalidResponseError,
    MessagingTimeoutError,
)

logger = logging.getLogger(__name__)


class MessagingClient:
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
            base_url=f"{config.effective_messaging_base_url.rstrip('/')}/",
            headers={"Accept": "application/json"},
            timeout=config.messaging_timeout_seconds,
        )

    async def __aenter__(self) -> "MessagingClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def respond_order(
        self,
        message_id: str,
        *,
        sku: str,
        quantity: int,
        delivery_date: datetime,
    ) -> MessagingResponse:
        request = MessagingOrderRequest(
            interpretation=MessagingOrderInterpretation(
                sku=sku,
                quantity=quantity,
                deliveryDate=delivery_date,
            )
        )
        response = await self._respond(
            message_id, request.model_dump(by_alias=True, mode="json")
        )
        if response.status not in (None, "order"):
            raise MessagingInvalidResponseError(
                "Messaging service returned a different response status"
            )
        if response.order_id is None and not response.test:
            raise MessagingInvalidResponseError(
                "Messaging service did not return the created order ID"
            )
        if response.order_id is not None:
            response.order_id = response.order_id.lower()
        return response

    async def respond_inquiry(
        self, message_id: str, *, order_id: str
    ) -> dict[str, Any]:
        request = MessagingInquiryRequest(orderId=order_id)
        request.order_id = request.order_id.lower()
        path = self._response_path(message_id)
        response = await self._request(
            "POST", path, json=request.model_dump(by_alias=True)
        )
        confirmation = self._confirmation(response)
        if confirmation.get("status") not in (None, "inquiry"):
            raise MessagingInvalidResponseError(
                "Messaging service returned a different inquiry status"
            )
        if "orderId" in confirmation:
            returned_id = confirmation["orderId"]
            if (
                not isinstance(returned_id, str)
                or returned_id.lower() != request.order_id
            ):
                raise MessagingInvalidResponseError(
                    "Messaging service returned a different inquiry order ID"
                )
        return confirmation

    async def send_reply(
        self, message: MessagingWebhookPayload, *, text: str
    ) -> dict[str, Any]:
        # Derive routing from the original query: callers cannot override it.
        request = MessagingSendRequest(
            to=message.sender.phone, replyTo=message.message_id, text=text
        )
        response = await self._request(
            "POST", "messages", json=request.model_dump(by_alias=True)
        )
        return self._confirmation(response)

    async def discard(self, message_id: str, *, reason: str) -> None:
        request = MessagingDiscardRequest(reason=reason)
        response = await self._respond(
            message_id, request.model_dump(by_alias=True, mode="json")
        )
        if response.order_id is not None or response.status != "discarded":
            raise MessagingInvalidResponseError(
                "Messaging service returned an invalid discard response"
            )

    async def _respond(
        self, message_id: str, payload: dict[str, Any]
    ) -> MessagingResponse:
        path = self._response_path(message_id)
        response = await self._request("POST", path, json=payload)
        if response.status_code != 200:
            raise MessagingInvalidResponseError(
                f"Messaging service returned unexpected HTTP {response.status_code}"
            )
        try:
            return MessagingResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise MessagingInvalidResponseError(
                "Messaging service returned invalid response data"
            ) from exc

    @staticmethod
    def _response_path(message_id: str) -> str:
        if not message_id.strip():
            raise ValueError("message_id must not be blank")
        return f"messages/{quote(message_id, safe='')}/response"

    @staticmethod
    def _confirmation(response: httpx.Response) -> dict[str, Any]:
        # E3 request bodies are specified in the assignment; the public API still
        # documents E2. Preserve confirmations without inventing response IDs.
        if not response.is_success:
            raise MessagingInvalidResponseError(
                f"Messaging service returned unexpected HTTP {response.status_code}"
            )
        if response.status_code == 204 and not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise MessagingInvalidResponseError(
                "Messaging service returned invalid confirmation JSON"
            ) from exc
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise MessagingInvalidResponseError(
                "Messaging service returned an invalid confirmation"
            )
        return payload

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
            "POST", "auth", json={"group": self._group, "secret": self._secret}
        )
        self._raise_for_status(response, authenticating=True)
        if response.status_code != 200:
            raise MessagingInvalidResponseError(
                "Messaging authentication returned unexpected "
                f"HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MessagingInvalidResponseError(
                "Messaging authentication returned invalid JSON"
            ) from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise MessagingInvalidResponseError(
                "Messaging authentication did not return a token"
            )
        return token

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http_client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise MessagingTimeoutError("Messaging request timed out") from exc
        except httpx.RequestError as exc:
            raise MessagingConnectionError(
                "Could not communicate with the messaging service"
            ) from exc

    @staticmethod
    def _raise_for_status(
        response: httpx.Response, *, authenticating: bool = False
    ) -> None:
        if not response.is_error:
            return
        logger.warning(
            "Messaging request failed: %s %s HTTP %s",
            response.request.method,
            response.request.url.path,
            response.status_code,
        )
        if response.status_code == 401 or (
            authenticating and response.status_code in (400, 403)
        ):
            raise MessagingAuthenticationError(response.status_code)
        if response.status_code == 409 and response.request.url.path.endswith(
            "/response"
        ):
            raise MessagingAlreadyRespondedError(response.status_code)
        retry_after = None
        if response.status_code == 429:
            with suppress(KeyError, ValueError):
                value = float(response.headers["Retry-After"])
                if math.isfinite(value):
                    retry_after = max(value, 0)
        raise MessagingHTTPError(response.status_code, retry_after_seconds=retry_after)
