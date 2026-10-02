from datetime import UTC, datetime

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.clients.farma_central_exceptions import FarmaCentralInvalidResponseError
from app.clients.market_prices import MarketPriceClient
from app.queries.catalog import list_sellable_catalog
from app.schemas.catalog import CatalogItemResponse, MarketPrice

MARKET_PRICES_ADAPTER = TypeAdapter(list[MarketPrice])


class CatalogPriceUnavailableError(Exception):
    """Raised when a sellable kit has no current market price."""


class CatalogService:
    def __init__(
        self,
        client: MarketPriceClient,
        session: Session,
    ) -> None:
        self._client = client
        self._session = session

    async def list_items(self) -> list[CatalogItemResponse]:
        price_payload = await self._client.get_current_prices()
        prices = self._parse_prices(price_payload)

        products = list_sellable_catalog(
            self._session,
            as_of=datetime.now(UTC),
        )

        missing_skus = [
            product.sku for product in products if product.sku not in prices
        ]
        if missing_skus:
            raise CatalogPriceUnavailableError(
                "Current market price is unavailable for: " + ", ".join(missing_skus)
            )

        return [
            CatalogItemResponse(
                sku=product.sku,
                name=product.name,
                price=prices[product.sku].price,
                stock=product.stock,
                price_updated_at=prices[product.sku].updated_at,
            )
            for product in products
        ]

    @staticmethod
    def _parse_prices(payload: object) -> dict[str, MarketPrice]:
        try:
            parsed_prices = MARKET_PRICES_ADAPTER.validate_python(payload)
        except ValidationError as exc:
            raise FarmaCentralInvalidResponseError(
                "The market price service returned invalid price data"
            ) from exc

        prices_by_sku: dict[str, MarketPrice] = {}

        for price in parsed_prices:
            if price.sku in prices_by_sku:
                raise FarmaCentralInvalidResponseError(
                    f"The market price service returned duplicate SKU {price.sku}"
                )

            prices_by_sku[price.sku] = price

        return prices_by_sku
