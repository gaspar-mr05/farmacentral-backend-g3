import httpx
import pytest

from app.db.health import database_available
from app.main import app


@pytest.mark.anyio
async def test_health_returns_ok() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
@pytest.mark.parametrize("available", [True, False])
async def test_readiness_reports_database_without_breaking_liveness(available):
    app.dependency_overrides[database_available] = lambda: available
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            ready = await client.get("/api/health/ready")
            live = await client.get("/api/health")
    finally:
        app.dependency_overrides.clear()
    assert ready.status_code == (200 if available else 503)
    state = "ok" if available else "unavailable"
    assert ready.json() == {"status": state, "database": state}
    assert ready.headers["cache-control"] == "no-store"
    assert live.status_code == 200 and live.json() == {"status": "ok"}
