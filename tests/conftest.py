# tests/conftest.py
import os
from collections.abc import Generator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://unused:unused@localhost:5432/unused",
)
os.environ.setdefault("FARMA_CENTRAL_BASE_URL", "https://example.test/")
os.environ.setdefault("FARMA_CENTRAL_API_SECRET", "unused-secret")
os.environ.setdefault("FARMA_CENTRAL_FTP", "unused-ftp")
os.environ.setdefault("FARMA_CENTRAL_GROUP", "3")

import app.models  # noqa: F401  — registra los modelos en Base.metadata
from app.db.base import Base
from app.db.session import get_session
from app.main import app

# Puede usar el Postgres de desarrollo: los tests crean su propio esquema temporal.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://farmacentral:change-me@localhost:5432/farmacentral",
)


@pytest.fixture(scope="session")
def test_engine():
    schema = f"pytest_{uuid4().hex}"
    admin_engine = create_engine(TEST_DATABASE_URL)
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    try:
        Base.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db_session(test_engine):
    """Sesión de DB envuelta en una transacción que se revierte al final del test."""
    with test_engine.connect() as connection:
        # El contexto de la transacción debe terminar siempre con rollback,
        # incluso cuando el código probado hace commit o el test falla.
        transaction = connection.begin()
        session = Session(
            bind=connection,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


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
