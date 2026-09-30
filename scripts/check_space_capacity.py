# scripts/check_space_capacity.py
import asyncio
from app.clients.farma_central import FarmaCentralClient


async def main():
    async with FarmaCentralClient() as client:
        spaces = await client.get_spaces()
        for space in spaces:
            print(f"{space['_id']}: {space['usedSpace']}/{space['totalSpace']} "
                  f"(buffer={space.get('buffer')}, packaging={space.get('packaging')})")


asyncio.run(main())