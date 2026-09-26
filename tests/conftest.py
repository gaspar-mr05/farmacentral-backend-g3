import os

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://unused:unused@localhost:5432/unused",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
