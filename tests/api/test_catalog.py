from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_market_price_client
from app.clients.farma_central_exceptions import FarmaCentralHTTPError
from app.main import app
from app.models import Location, Lot, LotOrigin, Product, ProductCategory, Unit


class FakeMarketPriceClient:
    def __init__(self, prices: list[dict]) -> None:
        self._prices = prices

    async def get_current_prices(self, *, use_cache: bool = True) -> list[dict]:
        return self._prices


def _override_market_prices(prices: list[dict]) -> None:
    async def override() -> AsyncGenerator[FakeMarketPriceClient, None]:
        yield FakeMarketPriceClient(prices)

    app.dependency_overrides[get_market_price_client] = override


def _add_unit(
    session: Session,
    *,
    lot: Lot,
    location: Location,
    expires_at: datetime,
    status: str = "available",
) -> None:
    session.add(
        Unit(
            external_unit_id=f"CATALOG-UNIT-{uuid4().hex}",
            lot_id=lot.id,
            current_location_id=location.id,
            status=status,
            effective_expires_at=expires_at,
        )
    )


def _price(sku: str, *, amount: int = 1000) -> dict:
    return {
        "sku": sku,
        "price": amount,
        "fairValue": amount,
        "updatedAt": "2026-10-02T12:00:00Z",
    }


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def test_catalog_combines_current_prices_with_sellable_stock(
    api_client: TestClient,
    db_session: Session,
) -> None:
    suffix = uuid4().hex
    now = datetime.now(UTC)
    kit = Product(
        sku=f"KIT-CATALOG-{suffix}",
        name=f"Kit catálogo {suffix}",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    empty_kit = Product(
        sku=f"KIT-EMPTY-{suffix}",
        name=f"Kit sin stock {suffix}",
        category=ProductCategory.KIT,
        batch_size=1,
        requires_refrigeration=False,
    )
    raw_material = Product(
        sku=f"RAW-{suffix}",
        name=f"Insumo {suffix}",
        category=ProductCategory.INSUMO,
        batch_size=1,
        requires_refrigeration=False,
    )
    warehouse = Location(
        code=f"WAREHOUSE-{suffix}",
        name="Almacenamiento",
        is_refrigerated=False,
        is_sellable=True,
    )
    quarantine = Location(
        code=f"QUARANTINE-{suffix}",
        name="Cuarentena",
        is_refrigerated=False,
        is_sellable=False,
    )
    db_session.add_all([kit, empty_kit, raw_material, warehouse, quarantine])
    db_session.flush()

    lot = Lot(
        external_lot_id=f"CATALOG-LOT-{suffix}",
        product_id=kit.id,
        expires_at=now + timedelta(days=90),
        origin=LotOrigin.OWN_PRODUCTION,
    )
    db_session.add(lot)
    db_session.flush()

    later_lot = Lot(
        external_lot_id=f"CATALOG-LATER-LOT-{suffix}",
        product_id=kit.id,
        expires_at=now + timedelta(days=120),
        origin=LotOrigin.OWN_PRODUCTION,
    )
    db_session.add(later_lot)
    db_session.flush()

    _add_unit(
        db_session,
        lot=lot,
        location=warehouse,
        expires_at=now + timedelta(days=30),
    )
    _add_unit(
        db_session,
        lot=lot,
        location=warehouse,
        expires_at=now + timedelta(days=30),
        status="unavailable",
    )
    _add_unit(
        db_session,
        lot=lot,
        location=warehouse,
        expires_at=now - timedelta(seconds=1),
    )
    _add_unit(
        db_session,
        lot=lot,
        location=quarantine,
        expires_at=now + timedelta(days=30),
    )
    _add_unit(
        db_session,
        lot=later_lot,
        location=warehouse,
        expires_at=now + timedelta(days=60),
    )
    _add_unit(
        db_session,
        lot=later_lot,
        location=warehouse,
        expires_at=now + timedelta(days=61),
    )
    db_session.flush()

    kit_skus = db_session.scalars(
        select(Product.sku).where(Product.category == ProductCategory.KIT)
    ).all()
    prices = [_price(sku) for sku in kit_skus]
    prices_by_sku = {price["sku"]: price for price in prices}
    prices_by_sku[kit.sku] = _price(kit.sku, amount=2500)
    prices_by_sku[empty_kit.sku] = _price(empty_kit.sku, amount=1800)
    _override_market_prices(list(prices_by_sku.values()))

    response = api_client.get("/api/catalog")

    assert response.status_code == 200
    items = {item["sku"]: item for item in response.json()}
    assert items[kit.sku] == {
        "sku": kit.sku,
        "name": kit.name,
        "price": 2500,
        "stock": 3,
        "next_expiry_at": _iso_z(now + timedelta(days=30)),
        "lots": [
            {
                "external_lot_id": lot.external_lot_id,
                "stock": 1,
                "next_expiry_at": _iso_z(now + timedelta(days=30)),
            },
            {
                "external_lot_id": later_lot.external_lot_id,
                "stock": 2,
                "next_expiry_at": _iso_z(now + timedelta(days=60)),
            },
        ],
        "price_updated_at": "2026-10-02T12:00:00Z",
    }
    assert items[empty_kit.sku]["stock"] == 0
    assert items[empty_kit.sku]["next_expiry_at"] is None
    assert items[empty_kit.sku]["lots"] == []
    assert raw_material.sku not in items


def test_catalog_returns_bad_gateway_when_a_kit_has_no_price(
    api_client: TestClient,
    db_session: Session,
) -> None:
    suffix = uuid4().hex
    db_session.add(
        Product(
            sku=f"KIT-NO-PRICE-{suffix}",
            name=f"Kit sin precio {suffix}",
            category=ProductCategory.KIT,
            batch_size=1,
            requires_refrigeration=False,
        )
    )
    db_session.flush()

    existing_skus = db_session.scalars(
        select(Product.sku).where(
            Product.category == ProductCategory.KIT,
            Product.sku != f"KIT-NO-PRICE-{suffix}",
        )
    ).all()
    _override_market_prices([_price(sku) for sku in existing_skus])

    response = api_client.get("/api/catalog")

    assert response.status_code == 502
    assert response.json() == {"detail": "Current market prices could not be obtained"}


def test_catalog_returns_service_unavailable_with_retry_after_when_rate_limited(
    api_client: TestClient,
) -> None:
    class RateLimitedMarketPriceClient:
        async def get_current_prices(self, *, use_cache: bool = True) -> list[dict]:
            raise FarmaCentralHTTPError(429, retry_after_seconds=12.2)

    async def override() -> AsyncGenerator[RateLimitedMarketPriceClient, None]:
        yield RateLimitedMarketPriceClient()

    app.dependency_overrides[get_market_price_client] = override

    response = api_client.get("/api/catalog")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "13"
    assert response.json() == {"detail": "The market price service is unavailable"}
