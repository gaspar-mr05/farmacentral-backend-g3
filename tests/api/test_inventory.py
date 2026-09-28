from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Location, Lot, LotOrigin, Product, ProductCategory, Unit


@dataclass(frozen=True)
class InventoryScenario:
    sku: str
    location_code: str
    available_unit_id: str
    unavailable_unit_id: str
    other_location_unit_id: str
    lot_id: str
    expires_at: datetime


@pytest.fixture
def inventory_scenario(db_session: Session) -> InventoryScenario:
    unique_id = uuid4().hex
    sku = f"API-INVENTORY-{unique_id}"
    location_code = f"API-LOCATION-{unique_id}"
    other_location_code = f"API-OTHER-LOCATION-{unique_id}"
    lot_id = f"API-LOT-{unique_id}"
    expires_at = datetime.now(UTC) + timedelta(days=90)

    product = Product(
        sku=sku,
        name="Inventario API",
        category=ProductCategory.INSUMO,
        batch_size=10,
        requires_refrigeration=False,
    )
    location = Location(
        code=location_code,
        name="Ubicación API",
        is_refrigerated=False,
    )
    other_location = Location(
        code=other_location_code,
        name="Otra ubicación API",
        is_refrigerated=True,
    )
    db_session.add_all([product, location, other_location])
    db_session.flush()

    lot = Lot(
        external_lot_id=lot_id,
        product_id=product.id,
        expires_at=expires_at,
        origin=LotOrigin.FARMA_CENTRAL,
    )
    db_session.add(lot)
    db_session.flush()

    available_unit_id = f"API-AVAILABLE-{unique_id}"
    unavailable_unit_id = f"API-UNAVAILABLE-{unique_id}"
    other_location_unit_id = f"API-OTHER-{unique_id}"
    db_session.add_all(
        [
            Unit(
                external_unit_id=available_unit_id,
                lot_id=lot.id,
                current_location_id=location.id,
                status="available",
                effective_expires_at=expires_at,
            ),
            Unit(
                external_unit_id=unavailable_unit_id,
                lot_id=lot.id,
                current_location_id=location.id,
                status="unavailable",
                effective_expires_at=expires_at,
            ),
            Unit(
                external_unit_id=other_location_unit_id,
                lot_id=lot.id,
                current_location_id=other_location.id,
                status="available",
                effective_expires_at=expires_at,
            ),
        ]
    )
    db_session.flush()

    return InventoryScenario(
        sku=sku,
        location_code=location_code,
        available_unit_id=available_unit_id,
        unavailable_unit_id=unavailable_unit_id,
        other_location_unit_id=other_location_unit_id,
        lot_id=lot_id,
        expires_at=expires_at,
    )


def test_available_inventory_excludes_unavailable_units(
    api_client: TestClient,
    inventory_scenario: InventoryScenario,
) -> None:
    response = api_client.get(
        "/api/inventory/available",
        params={"sku": inventory_scenario.sku},
    )

    assert response.status_code == 200
    body = response.json()
    unit_ids = {item["unit"]["external_unit_id"] for item in body}
    assert unit_ids == {
        inventory_scenario.available_unit_id,
        inventory_scenario.other_location_unit_id,
    }
    assert inventory_scenario.unavailable_unit_id not in unit_ids


def test_available_inventory_filters_by_sku_and_location(
    api_client: TestClient,
    inventory_scenario: InventoryScenario,
) -> None:
    response = api_client.get(
        "/api/inventory/available",
        params={
            "sku": inventory_scenario.sku,
            "location_code": inventory_scenario.location_code,
        },
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "unit": {
                "external_unit_id": inventory_scenario.available_unit_id,
                "status": "available",
            },
            "product": {
                "sku": inventory_scenario.sku,
                "name": "Inventario API",
                "category": "insumo",
                "batch_size": 10,
                "requires_refrigeration": False,
            },
            "lot": {
                "external_lot_id": inventory_scenario.lot_id,
                "expires_at": inventory_scenario.expires_at.isoformat().replace(
                    "+00:00", "Z"
                ),
            },
            "location": {
                "code": inventory_scenario.location_code,
                "name": "Ubicación API",
                "is_refrigerated": False,
            },
        }
    ]


def test_available_inventory_returns_empty_list_when_there_are_no_matches(
    api_client: TestClient,
) -> None:
    response = api_client.get(
        "/api/inventory/available",
        params={"sku": f"MISSING-{uuid4().hex}"},
    )

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("parameter", ["sku", "location_code"])
def test_available_inventory_rejects_empty_filters(
    api_client: TestClient,
    parameter: str,
) -> None:
    response = api_client.get(
        "/api/inventory/available",
        params={parameter: ""},
    )

    assert response.status_code == 422
