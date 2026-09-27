import asyncio
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient, JSONResponse
from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.db.products import get_product_by_sku
from app.models import ProductCategory
from app.schemas.farma_central import (
    FarmaCentralChallengeResponse,
    FarmaCentralSupplyResponse,
)
from app.schemas.supply import SupplyResponse
from app.services.supply.proof_of_work import solve_challenge


class SupplyProductNotFoundError(Exception):
    def __init__(self, sku: str) -> None:
        super().__init__(f"Product {sku} was not found")


class InvalidSupplyRequestError(Exception):
    pass


class ChallengeExpiredError(Exception):
    pass


class SupplyRequestService:
    def __init__(self, client: FarmaCentralClient, session: Session) -> None:
        self._client = client
        self._session = session

    async def request(self, *, sku: str, quantity: int) -> SupplyResponse:
        product = get_product_by_sku(self._session, sku)
        if product is None:
            raise SupplyProductNotFoundError(sku)
        if product.category != ProductCategory.INSUMO:
            raise InvalidSupplyRequestError("Only supplies can be requested")
        if quantity % product.batch_size != 0:
            raise InvalidSupplyRequestError(
                f"Quantity must be a multiple of {product.batch_size}"
            )

        challenge = _parse_response(
            await self._client.request_fabrication_challenge(sku, quantity),
            FarmaCentralChallengeResponse,
        )
        if challenge.sku != sku or challenge.quantity != quantity:
            raise FarmaCentralInvalidResponseError(
                "Farma Central returned a challenge for another request"
            )
        if _is_expired(challenge.expires_at):
            raise ChallengeExpiredError("The fabrication challenge expired")

        nonce = await asyncio.to_thread(
            solve_challenge,
            challenge.prefix,
            challenge.difficulty,
        )
        if _is_expired(challenge.expires_at):
            raise ChallengeExpiredError("The fabrication challenge expired")

        result = _parse_response(
            await self._client.request_products(
                sku=sku,
                quantity=quantity,
                challenge_id=challenge.challenge_id,
                nonce=nonce,
            ),
            FarmaCentralSupplyResponse,
        )
        if result.sku != sku or result.quantity != quantity:
            raise FarmaCentralInvalidResponseError(
                "Farma Central returned a result for another request"
            )

        return SupplyResponse(
            sku=result.sku,
            quantity=result.quantity,
            available_at=result.available_at,
        )


def _parse_response[Schema: BaseModel](
    payload: JSONResponse,
    schema: type[Schema],
) -> Schema:
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise FarmaCentralInvalidResponseError(
            "Farma Central returned an invalid supply response"
        ) from exc


def _is_expired(expires_at: datetime) -> bool:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)
