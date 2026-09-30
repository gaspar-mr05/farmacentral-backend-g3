# scripts/farma_central_common.py
from app.clients.farma_central import FarmaCentralClient


async def get_buffer_and_packaging(client: FarmaCentralClient) -> tuple[dict, dict]:
    spaces = await client.get_spaces()
    buffer_space = next(s for s in spaces if s.get("buffer"))
    packaging_space = next(s for s in spaces if s.get("packaging"))
    return buffer_space, packaging_space