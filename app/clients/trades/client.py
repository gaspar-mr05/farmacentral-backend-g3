import asyncio
from contextlib import suppress
from decimal import Decimal
from typing import Any

import httpx
from pydantic import TypeAdapter, ValidationError

from app.core.config import Settings, get_settings
from app.schemas.trades import (
    TradeOffer,
    TradeOfferCreate,
    TradeOfferId,
    TradeSide,
    TradeState,
)

from .exceptions import (
    TradesAuthenticationError,
    TradesConnectionError,
    TradesHTTPError,
    TradesInvalidResponseError,
    TradesTimeoutError,
    TradesUncertainResultError,
)

OFFER_ID_ADAPTER = TypeAdapter(TradeOfferId)


class TradesClient:
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
            base_url=f"{config.effective_trades_base_url.rstrip('/')}/",
            headers={"Accept": "application/json"},
            timeout=config.trades_timeout_seconds,
        )

    async def __aenter__(self) -> "TradesClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def list_offers(
        self,
        *,
        sku: str | None = None,
        side: TradeSide | None = None,
        state: TradeState | None = None,
    ) -> list[TradeOffer]:
        params = {
            key: value
            for key, value in {"sku": sku, "side": side, "state": state}.items()
            if value is not None
        }
        response = await self._request("GET", "offers", params=params)
        payload = self._decode_json(response)
        if isinstance(payload, dict):
            payload = payload.get("offers", payload.get("data"))
        if not isinstance(payload, list):
            raise TradesInvalidResponseError("Trades did not return an offer list")
        return self._validate_offers(payload)

    async def get_offer(self, offer_id: str) -> TradeOffer:
        offer_id = OFFER_ID_ADAPTER.validate_python(offer_id).lower()
        response = await self._request("GET", f"offers/{offer_id}")
        return self._validate_offer_response(response, expected_id=offer_id)

    async def create_offer(
        self,
        offer: TradeOfferCreate,
        *,
        current_unit_price: Decimal,
    ) -> TradeOffer:
        offer.validate_market_price(current_unit_price)
        response = await self._request(
            "POST",
            "offers",
            json=offer.model_dump(mode="json", by_alias=True),
            result_may_be_uncertain=True,
        )
        return self._mutation_offer(response)

    async def accept_offer(self, offer_id: str) -> TradeOffer:
        offer_id = OFFER_ID_ADAPTER.validate_python(offer_id).lower()
        response = await self._request(
            "POST",
            f"offers/{offer_id}/accept",
            result_may_be_uncertain=True,
        )
        offer = self._mutation_offer(response, expected_id=offer_id)
        if offer.status != "closed" or not offer.order_id or offer.closed_at is None:
            raise TradesUncertainResultError(
                "Acceptance returned incomplete closure evidence; "
                "reconcile before retrying"
            )
        return offer

    async def cancel_offer(self, offer_id: str) -> TradeOffer | None:
        offer_id = OFFER_ID_ADAPTER.validate_python(offer_id).lower()
        response = await self._request(
            "POST",
            f"offers/{offer_id}/cancel",
            result_may_be_uncertain=True,
        )
        if response.status_code == 204 or not response.content:
            return None
        return self._mutation_offer(response, expected_id=offer_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        result_may_be_uncertain: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        if method == "GET":
            response = await self._send(method, path, **kwargs)
            self._raise_for_status(response)
            return response
        token = await self._get_token()
        response = await self._send(
            method,
            path,
            token=token,
            result_may_be_uncertain=result_may_be_uncertain,
            **kwargs,
        )
        if response.status_code == 401:
            token = await self._refresh_token(token)
            response = await self._send(
                method,
                path,
                token=token,
                result_may_be_uncertain=result_may_be_uncertain,
                **kwargs,
            )
        if result_may_be_uncertain and (
            response.status_code >= 500
            or response.status_code == 408
            or 300 <= response.status_code < 400
        ):
            raise TradesUncertainResultError(
                "Trades may have processed the request; reconcile before retrying",
                status_code=response.status_code,
            )
        self._raise_for_status(response)
        return response

    @classmethod
    def _mutation_offer(cls, response, *, expected_id=None):
        try:
            return cls._validate_offer_response(response, expected_id=expected_id)
        except TradesInvalidResponseError:
            raise TradesUncertainResultError(
                "Mutation returned invalid offer data; reconcile before retrying",
                status_code=response.status_code,
            ) from None

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
            "POST",
            "autenticar",
            json={"group": self._group, "secret": self._secret},
        )
        self._raise_for_status(response, authenticating=True)
        payload = self._decode_json(response)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise TradesInvalidResponseError(
                "Trades authentication did not return a token"
            )
        return token

    async def _send(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        result_may_be_uncertain: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            return await self._http_client.request(
                method, path, headers=headers, **kwargs
            )
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ) as exc:
            if result_may_be_uncertain:
                raise TradesUncertainResultError(
                    "Trades may have processed the request; reconcile before retrying"
                ) from exc
            raise TradesTimeoutError("Trades request did not complete") from exc
        except httpx.TimeoutException as exc:
            raise TradesTimeoutError("Trades request timed out") from exc
        except httpx.RequestError as exc:
            raise TradesConnectionError("Could not communicate with Trades") from exc

    @staticmethod
    def _raise_for_status(
        response: httpx.Response, *, authenticating: bool = False
    ) -> None:
        if not response.is_error:
            return
        if response.status_code == 401 or (
            authenticating and response.status_code in (400, 403)
        ):
            raise TradesAuthenticationError(response.status_code)
        retry_after = None
        if response.status_code == 429:
            with suppress(KeyError, ValueError):
                retry_after = max(float(response.headers["Retry-After"]), 0)
        raise TradesHTTPError(response.status_code, retry_after_seconds=retry_after)

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise TradesInvalidResponseError("Trades returned invalid JSON") from exc

    @classmethod
    def _validate_offer_response(
        cls, response: httpx.Response, *, expected_id: str | None = None
    ) -> TradeOffer:
        payload = cls._decode_json(response)
        if isinstance(payload, dict):
            payload = payload.get("offer", payload.get("data", payload))
        try:
            offer = TradeOffer.model_validate(payload)
        except ValidationError as exc:
            raise TradesInvalidResponseError(
                "Trades returned invalid offer data"
            ) from exc
        if expected_id is not None and offer.id.lower() != expected_id:
            raise TradesInvalidResponseError("Trades returned a different offer ID")
        return offer

    @staticmethod
    def _validate_offers(payload: list[Any]) -> list[TradeOffer]:
        try:
            return TypeAdapter(list[TradeOffer]).validate_python(payload)
        except ValidationError as exc:
            raise TradesInvalidResponseError(
                "Trades returned invalid offer data"
            ) from exc
