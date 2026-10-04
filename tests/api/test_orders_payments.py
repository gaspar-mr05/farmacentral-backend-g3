from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_checkout_client, get_market_price_client
from app.api.routes import payments as payment_routes
from app.core.config import Settings
from app.main import app
from app.models import (
    Location,
    Lot,
    LotOrigin,
    Order,
    OrderItem,
    OrderSource,
    OrderStatus,
    PaymentStatus,
    Product,
    ProductCategory,
    Unit,
)


class FakeMarketPriceClient:
    def __init__(self, prices: list[dict]) -> None:
        self._prices = prices

    async def get_current_prices(self) -> list[dict]:
        return self._prices


class FakeCheckoutClient:
    def __init__(self, *, external_status: str = "PENDING") -> None:
        self.external_status = external_status
        self.initialize_requests: list[dict] = []
        self.status_requests = 0

    async def initialize_payment(self, *, amount: int, back_urls: dict) -> dict:
        self.initialize_requests.append({"amount": amount, "back_urls": back_urls})
        return {
            "payment_id": f"external-{uuid4().hex}",
            "redirect_url": "https://checkout.test/session",
        }

    async def get_payment(self, payment_id: str) -> dict:
        self.status_requests += 1
        return {"amount": 3000, "group": 3, "status": self.external_status}


def _override_market_prices(session: Session, price_by_sku: dict[str, int]) -> None:
    skus = session.scalars(
        select(Product.sku).where(Product.category == ProductCategory.KIT)
    ).all()
    prices = [
        {
            "sku": sku,
            "price": price_by_sku.get(sku, 1000),
            "fairValue": price_by_sku.get(sku, 1000),
            "updatedAt": "2026-10-02T12:00:00Z",
        }
        for sku in skus
    ]

    async def override() -> AsyncGenerator[FakeMarketPriceClient, None]:
        yield FakeMarketPriceClient(prices)

    app.dependency_overrides[get_market_price_client] = override


def _add_sellable_kit(session: Session, *, stock: int = 2) -> Product:
    suffix = uuid4().hex
    kit = Product(
        sku=f"KIT-ORDER-{suffix}",
        name=f"Kit pedido {suffix}",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    location = Location(
        code=f"ORDER-WAREHOUSE-{suffix}",
        name="Bodega de pedidos",
        is_refrigerated=False,
        is_sellable=True,
    )
    session.add_all([kit, location])
    session.flush()
    lot = Lot(
        external_lot_id=f"ORDER-LOT-{suffix}",
        product_id=kit.id,
        expires_at=datetime.now(UTC) + timedelta(days=90),
        origin=LotOrigin.OWN_PRODUCTION,
    )
    session.add(lot)
    session.flush()
    session.add_all(
        [
            Unit(
                external_unit_id=f"ORDER-UNIT-{suffix}-{index}",
                lot_id=lot.id,
                current_location_id=location.id,
                status="available",
                effective_expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            for index in range(stock)
        ]
    )
    session.flush()
    return kit


def _add_order(session: Session) -> Order:
    order = Order(
        buyer_name="Ada Lovelace",
        buyer_email="ada@example.com",
        source=OrderSource.WEB,
        status=OrderStatus.PENDING_PAYMENT,
        total=3000,
        items=[OrderItem(sku="KIT-PAYMENT", quantity=2, unit_price=1500)],
    )
    session.add(order)
    session.flush()
    return order


def _override_checkout(client: FakeCheckoutClient) -> None:
    async def override() -> AsyncGenerator[FakeCheckoutClient, None]:
        yield client

    app.dependency_overrides[get_checkout_client] = override


def test_create_order_calculates_total_with_current_price(
    api_client: TestClient,
    db_session: Session,
) -> None:
    kit = _add_sellable_kit(db_session)
    _override_market_prices(db_session, {kit.sku: 1750})

    response = api_client.post(
        "/api/orders",
        json={
            "buyer_name": "Ada Lovelace",
            "buyer_email": "ada@example.com",
            "items": [{"sku": kit.sku, "quantity": 2}],
            "total": 1,
        },
    )

    assert response.status_code == 201
    assert response.json()["total"] == 3500
    assert response.json()["items"][0]["unit_price"] == 1750
    assert response.json()["status"] == "pending_payment"


def test_create_order_rejects_invalid_quantity(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/orders",
        json={
            "buyer_name": "Ada Lovelace",
            "buyer_email": "ada@example.com",
            "items": [{"sku": "KIT-1", "quantity": 0}],
        },
    )

    assert response.status_code == 422


def test_create_order_rejects_insufficient_stock(
    api_client: TestClient,
    db_session: Session,
) -> None:
    kit = _add_sellable_kit(db_session, stock=1)
    _override_market_prices(db_session, {kit.sku: 1750})

    response = api_client.post(
        "/api/orders",
        json={
            "buyer_name": "Ada Lovelace",
            "buyer_email": "ada@example.com",
            "items": [{"sku": kit.sku, "quantity": 2}],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == f"Insufficient stock for {kit.sku}"


def test_successful_payment_is_verified_and_confirmation_is_idempotent(
    api_client: TestClient,
    db_session: Session,
) -> None:
    order = _add_order(db_session)
    checkout = FakeCheckoutClient(external_status="SUCCESS")
    _override_checkout(checkout)

    start_response = api_client.post(f"/api/orders/{order.id}/payments")
    payment_id = start_response.json()["id"]
    first_callback = api_client.get(f"/api/payments/{payment_id}/return/success")
    checkout.external_status = "ERROR"
    repeated_callback = api_client.get(f"/api/payments/{payment_id}/return/cancelled")

    assert start_response.status_code == 201
    assert checkout.initialize_requests[0]["amount"] == 3000
    assert first_callback.json()["status"] == "success"
    assert repeated_callback.json()["status"] == "success"
    assert checkout.status_requests == 1
    db_session.refresh(order)
    assert order.status == OrderStatus.PAID


@pytest.mark.parametrize(
    ("external_status", "expected_payment_status", "expected_order_status"),
    [
        ("CANCELLED", PaymentStatus.CANCELLED, OrderStatus.CANCELLED),
        ("ERROR", PaymentStatus.ERROR, OrderStatus.PAYMENT_ERROR),
        ("OBSOLETE", PaymentStatus.OBSOLETE, OrderStatus.PAYMENT_ERROR),
    ],
)
def test_payment_final_states_update_the_order(
    api_client: TestClient,
    db_session: Session,
    external_status: str,
    expected_payment_status: PaymentStatus,
    expected_order_status: OrderStatus,
) -> None:
    order = _add_order(db_session)
    checkout = FakeCheckoutClient(external_status=external_status)
    _override_checkout(checkout)
    payment_id = api_client.post(f"/api/orders/{order.id}/payments").json()["id"]

    response = api_client.get(f"/api/payments/{payment_id}/return/success")

    assert response.status_code == 200
    assert response.json()["status"] == expected_payment_status.value
    db_session.refresh(order)
    assert order.status == expected_order_status


def test_payment_return_redirects_to_frontend_when_configured(
    api_client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _add_order(db_session)
    checkout = FakeCheckoutClient(external_status="SUCCESS")
    _override_checkout(checkout)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused:unused@localhost/unused",
        farma_central_base_url="https://example.test/api/",
        farma_central_api_secret="test-secret",
        farma_central_ftp="test-ftp",
        farma_central_group=3,
        frontend_public_url="https://frontend.test/",
    )
    monkeypatch.setattr(payment_routes, "get_settings", lambda: settings)
    payment_id = api_client.post(f"/api/orders/{order.id}/payments").json()["id"]

    response = api_client.get(
        f"/api/payments/{payment_id}/return/success",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith(
        "https://frontend.test/payment-result?"
    )
