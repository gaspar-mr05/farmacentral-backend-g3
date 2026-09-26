import os

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://unused:unused@localhost:5432/unused",
)
os.environ.setdefault("FARMA_CENTRAL_BASE_URL", "https://example.test/")
os.environ.setdefault("FARMA_CENTRAL_API_SECRET", "unused-secret")
os.environ.setdefault("FARMA_CENTRAL_FTP", "unused-ftp")
os.environ.setdefault("FARMA_CENTRAL_GROUP", "3")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
