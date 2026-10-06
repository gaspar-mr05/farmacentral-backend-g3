from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.clients.farma_central_exceptions import FarmaCentralHTTPError
from app.models import Location, Lot, LotOrigin, Product, ProductCategory, Unit
from app.services.production.orchestration import (
    InvalidProductionQuantityError,
    ProductionInputsUnavailableError,
    produce,
)


def _production_client(
    *, target_sku: str, component_sku: str, now: datetime
) -> AsyncMock:
    client = AsyncMock()
    client.get_available_products.return_value = [
        {
            "sku": target_sku,
            "name": "Output",
            "production": {"batch": 3, "at": "packaging"},
            "sellable": False,
            "components": [{"sku": component_sku, "req": 2}],
        }
    ]
    client.get_spaces.return_value = [{"_id": "PACKAGING", "packaging": True}]
    client.request_fabrication_challenge.return_value = {
        "challengeId": "challenge-123",
        "prefix": "abc123",
        "algorithm": "sha256-leading-zero-bits",
        "difficulty": 0,
        "sku": target_sku,
        "quantity": 3,
        "expiresAt": (now + timedelta(minutes=5)).isoformat(),
    }
    client.request_products.return_value = {
        "sku": target_sku,
        "group": 3,
        "quantity": 3,
        "availableAt": (now + timedelta(minutes=10)).isoformat(),
    }
    return client


def _create_inputs(db_session, *, component_sku: str, count: int) -> list[Unit]:
    location = Location(
        code="PACKAGING", name="Acondicionamiento", is_refrigerated=False
    )
    product = Product(
        sku=component_sku,
        name="Input",
        category=ProductCategory.INSUMO,
        batch_size=10,
        requires_refrigeration=False,
    )
    db_session.add_all([location, product])
    db_session.flush()
    lot = Lot(
        external_lot_id=f"LOT-{uuid4().hex}",
        product_id=product.id,
        origin=LotOrigin.FARMA_CENTRAL,
    )
    db_session.add(lot)
    db_session.flush()
    units = [
        Unit(
            external_unit_id=f"UNIT-{uuid4().hex}",
            lot_id=lot.id,
            current_location_id=location.id,
            status="available",
            effective_expires_at=datetime.now(UTC) + timedelta(days=100),
        )
        for _ in range(count)
    ]
    db_session.add_all(units)
    db_session.flush()
    return units


@pytest.mark.anyio
async def test_produce_consumes_formula_requirement_per_output_unit(db_session):
    now = datetime.now(UTC)
    suffix = uuid4().hex
    target_sku = f"OUTPUT-{suffix}"
    component_sku = f"INPUT-{suffix}"
    units = _create_inputs(db_session, component_sku=component_sku, count=6)
    client = _production_client(
        target_sku=target_sku, component_sku=component_sku, now=now
    )

    run, supply = await produce(
        db_session,
        client=client,
        sku=target_sku,
        quantity=3,
    )

    assert run.expected_sku == target_sku
    assert run.expected_quantity == 3
    assert run.available_at == supply.available_at
    assert sum(item.quantity_consumed for item in run.inputs) == 6
    assert all(unit.status == "consumed" for unit in units)
    client.request_products.assert_awaited_once()


@pytest.mark.anyio
async def test_produce_rejects_missing_inputs_before_requesting_challenge(db_session):
    now = datetime.now(UTC)
    suffix = uuid4().hex
    target_sku = f"OUTPUT-{suffix}"
    component_sku = f"INPUT-{suffix}"
    _create_inputs(db_session, component_sku=component_sku, count=5)
    client = _production_client(
        target_sku=target_sku, component_sku=component_sku, now=now
    )

    with pytest.raises(ProductionInputsUnavailableError) as exc_info:
        await produce(db_session, client=client, sku=target_sku, quantity=3)

    assert exc_info.value.missing_by_sku == {component_sku: 1}
    client.request_fabrication_challenge.assert_not_awaited()


@pytest.mark.anyio
async def test_produce_rejects_quantity_outside_batch_size(db_session):
    now = datetime.now(UTC)
    suffix = uuid4().hex
    target_sku = f"OUTPUT-{suffix}"
    component_sku = f"INPUT-{suffix}"
    client = _production_client(
        target_sku=target_sku, component_sku=component_sku, now=now
    )

    with pytest.raises(InvalidProductionQuantityError):
        await produce(db_session, client=client, sku=target_sku, quantity=2)

    client.get_spaces.assert_not_awaited()
    client.request_fabrication_challenge.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [400, 409])
async def test_produce_renews_rejected_challenge_once(db_session, status_code):
    now = datetime.now(UTC)
    suffix = uuid4().hex
    target_sku = f"OUTPUT-{suffix}"
    component_sku = f"INPUT-{suffix}"
    _create_inputs(db_session, component_sku=component_sku, count=6)
    client = _production_client(
        target_sku=target_sku,
        component_sku=component_sku,
        now=now,
    )
    successful_response = client.request_products.return_value
    client.request_products.side_effect = [
        FarmaCentralHTTPError(status_code),
        successful_response,
    ]

    run, _ = await produce(
        db_session,
        client=client,
        sku=target_sku,
        quantity=3,
    )

    assert run.expected_quantity == 3
    assert client.request_fabrication_challenge.await_count == 2
    assert client.request_products.await_count == 2


@pytest.mark.anyio
async def test_produce_does_not_retry_non_challenge_http_error(db_session):
    now = datetime.now(UTC)
    suffix = uuid4().hex
    target_sku = f"OUTPUT-{suffix}"
    component_sku = f"INPUT-{suffix}"
    _create_inputs(db_session, component_sku=component_sku, count=6)
    client = _production_client(
        target_sku=target_sku,
        component_sku=component_sku,
        now=now,
    )
    client.request_products.side_effect = FarmaCentralHTTPError(500)

    with pytest.raises(FarmaCentralHTTPError):
        await produce(
            db_session,
            client=client,
            sku=target_sku,
            quantity=3,
        )

    client.request_fabrication_challenge.assert_awaited_once()
    client.request_products.assert_awaited_once()
