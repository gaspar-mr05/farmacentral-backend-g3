from collections.abc import AsyncGenerator
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import get_farma_central_client
from app.main import app
from app.models import Product, ProductCategory


class FakeSupplyClient:
    async def request_fabrication_challenge(self, sku: str, quantity: int):
        return {
            "challengeId": "challenge-1",
            "prefix": "prefix",
            "algorithm": "sha256-leading-zero-bits",
            "difficulty": 0,
            "sku": sku,
            "quantity": quantity,
            "expiresAt": "2099-01-01T00:00:00Z",
        }

    async def request_products(
        self,
        *,
        sku: str,
        quantity: int,
        challenge_id: str,
        nonce: str,
    ):
        return {
            "sku": sku,
            "group": 3,
            "quantity": quantity,
            "availableAt": "2099-01-02T00:00:00Z",
        }


def test_supply_request_endpoint_returns_availability(
    api_client: TestClient,
    db_session: Session,
) -> None:
    product = Product(
        sku=f"API-SUPPLY-{uuid4().hex}",
        name="API supply product",
        category=ProductCategory.INSUMO,
        batch_size=10,
        requires_refrigeration=False,
    )
    db_session.add(product)
    db_session.flush()

    async def override_client() -> AsyncGenerator[FakeSupplyClient, None]:
        yield FakeSupplyClient()

    app.dependency_overrides[get_farma_central_client] = override_client

    response = api_client.post(
        "/api/supply-requests",
        json={"sku": product.sku, "quantity": 20},
    )

    assert response.status_code == 201
    assert response.json() == {
        "sku": product.sku,
        "quantity": 20,
        "available_at": "2099-01-02T00:00:00Z",
    }
