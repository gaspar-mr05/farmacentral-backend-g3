from collections.abc import AsyncGenerator

from app.clients.farma_central import FarmaCentralClient


async def get_farma_central_client() -> AsyncGenerator[FarmaCentralClient, None]:
    async with FarmaCentralClient() as client:
        yield client
