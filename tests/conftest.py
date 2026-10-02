# tests/conftest.py
import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://unused:unused@localhost:5432/unused",
)
os.environ.setdefault("FARMA_CENTRAL_BASE_URL", "https://example.test/")
os.environ.setdefault("FARMA_CENTRAL_API_SECRET", "unused-secret")
os.environ.setdefault("FARMA_CENTRAL_FTP", "unused-ftp")
os.environ.setdefault("FARMA_CENTRAL_GROUP", "3")

import app.models  # noqa: F401  — registra los modelos en Base.metadata
from app.db.session import get_session
from app.main import app

# Apunta al Postgres real de docker compose (mismo que usas en desarrollo,
# por ahora no hay una DB de test separada).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://farmacentral:change-me@localhost:5432/farmacentral",
)

engine = create_engine(TEST_DATABASE_URL)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db_session():
    """Sesión de DB envuelta en una transacción que se revierte al final del test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(
        bind=connection,
        join_transaction_mode="create_savepoint",
    )

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def api_client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_session() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
