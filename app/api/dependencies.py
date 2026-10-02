from collections.abc import AsyncGenerator

from app.clients.checkout import CheckoutClient
from app.clients.farma_central import FarmaCentralClient
from app.clients.market_prices import MarketPriceClient


async def get_farma_central_client() -> AsyncGenerator[FarmaCentralClient, None]:
    async with FarmaCentralClient() as client:
        yield client


async def get_market_price_client() -> AsyncGenerator[MarketPriceClient, None]:
    async with MarketPriceClient() as client:
        yield client


async def get_checkout_client() -> AsyncGenerator[CheckoutClient, None]:
    async with CheckoutClient() as client:
        yield client
