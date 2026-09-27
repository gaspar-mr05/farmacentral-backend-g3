from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import Product, ProductCategory
from app.services.supply.requests import (
    InvalidSupplyRequestError,
    SupplyRequestService,
)


class FakeSupplyClient:
    def __init__(self) -> None:
        self.challenge_calls: list[tuple[str, int]] = []
        self.product_calls: list[dict[str, str | int]] = []

    async def request_fabrication_challenge(self, sku: str, quantity: int):
        self.challenge_calls.append((sku, quantity))
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
        self.product_calls.append(
            {
                "sku": sku,
                "quantity": quantity,
                "challenge_id": challenge_id,
                "nonce": nonce,
            }
        )
        return {
            "sku": sku,
            "group": 3,
            "quantity": quantity,
            "availableAt": "2099-01-02T00:00:00Z",
        }


def _create_supply_product(session: Session, *, batch_size: int = 10) -> Product:
    product = Product(
        sku=f"SUPPLY-{uuid4().hex}",
        name="Supply product",
        category=ProductCategory.INSUMO,
        batch_size=batch_size,
        requires_refrigeration=False,
    )
    session.add(product)
    session.flush()
    return product


@pytest.mark.anyio
async def test_request_completes_challenge_and_returns_availability(
    db_session: Session,
) -> None:
    product = _create_supply_product(db_session)
    client = FakeSupplyClient()

    result = await SupplyRequestService(client, db_session).request(
        sku=product.sku,
        quantity=20,
    )

    assert result.sku == product.sku
    assert result.quantity == 20
    assert result.available_at == datetime(2099, 1, 2, tzinfo=UTC)
    assert client.challenge_calls == [(product.sku, 20)]
    assert client.product_calls[0]["challenge_id"] == "challenge-1"


@pytest.mark.anyio
async def test_request_rejects_quantity_outside_batch_size(
    db_session: Session,
) -> None:
    product = _create_supply_product(db_session, batch_size=10)
    client = FakeSupplyClient()

    with pytest.raises(InvalidSupplyRequestError):
        await SupplyRequestService(client, db_session).request(
            sku=product.sku,
            quantity=15,
        )

    assert client.challenge_calls == []
