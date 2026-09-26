# tests/conftest.py
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://unused:unused@localhost:5432/unused",
)

import app.models  # noqa: F401  — registra los modelos en Base.metadata

# Apunta al Postgres real de docker compose (mismo que usas en desarrollo,
# por ahora no hay una DB de test separada).
TEST_DATABASE_URL = (
    "postgresql+psycopg://farmacentral:change-me@localhost:5432/farmacentral"
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
    session = TestingSessionLocal(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()
