from collections.abc import AsyncGenerator

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import get_farma_central_client
from app.main import app
from tests.support.inventory import create_movement_scenario


class FakeMovementClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def move_product(self, product_id: str, store_id: str) -> None:
        self.calls.append((product_id, store_id))


def test_move_product_endpoint_returns_confirmed_movement(
    api_client: TestClient,
    db_session: Session,
) -> None:
    scenario = create_movement_scenario(db_session)
    client = FakeMovementClient()

    async def override_client() -> AsyncGenerator[FakeMovementClient, None]:
        yield client

    app.dependency_overrides[get_farma_central_client] = override_client

    response = api_client.patch(
        f"/api/products/{scenario.product_id}",
        json={"store": scenario.destination_code},
    )

    assert response.status_code == 200
    assert response.json() == {
        "product_id": scenario.product_id,
        "from_store": scenario.origin_code,
        "to_store": scenario.destination_code,
        "moved": True,
    }
    assert client.calls == [(scenario.product_id, scenario.destination_code)]


def test_move_product_endpoint_rejects_blank_store(api_client: TestClient) -> None:
    response = api_client.patch(
        "/api/products/a-product-id",
        json={"store": "   "},
    )

    assert response.status_code == 422
