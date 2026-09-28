from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.clients.farma_central_exceptions import FarmaCentralHTTPError
from app.models import (
    CustodyEvent,
    CustodyEventType,
    Location,
    Lot,
    LotOrigin,
    Product,
    ProductCategory,
    Unit,
)
from app.services.inventory.cold_chain import (
    ColdChainService,
    NoRefrigeratedLocationError,
)


class FakeMovementClient:
    def __init__(
        self,
        error: Exception | None = None,
        errors_by_store: dict[str, Exception] | None = None,
    ) -> None:
        self.error = error
        self.errors_by_store = errors_by_store or {}
        self.calls: list[tuple[str, str]] = []

    async def move_product(self, product_id: str, store_id: str) -> None:
        self.calls.append((product_id, store_id))
        if store_id in self.errors_by_store:
            raise self.errors_by_store[store_id]
        if self.error is not None:
            raise self.error


@dataclass(frozen=True)
class ColdChainScenario:
    unit: Unit
    origin: Location
    destination: Location | None


def create_cold_chain_scenario(
    session: Session,
    *,
    requires_refrigeration: bool = True,
    include_refrigerated_destination: bool = True,
) -> ColdChainScenario:
    suffix = uuid4().hex
    expires_at = datetime.now(UTC) + timedelta(days=30)
    session.execute(update(Location).values(is_refrigerated=False))
    product = Product(
        sku=f"COLD-CHAIN-{suffix}",
        name="Cold chain test product",
        category=ProductCategory.INSUMO,
        batch_size=10,
        requires_refrigeration=requires_refrigeration,
    )
    origin = Location(
        code=f"ambient-{suffix}",
        name="Ambient storage",
        is_refrigerated=False,
    )
    destination = None
    locations = [origin]
    if include_refrigerated_destination:
        destination = Location(
            code=f"cold-{suffix}",
            name="Cold storage",
            is_refrigerated=True,
        )
        locations.append(destination)

    session.add_all([product, *locations])
    session.flush()

    lot = Lot(
        external_lot_id=f"COLD-LOT-{suffix}",
        product_id=product.id,
        expires_at=expires_at,
        origin=LotOrigin.FARMA_CENTRAL,
    )
    session.add(lot)
    session.flush()

    unit = Unit(
        external_unit_id=f"COLD-UNIT-{suffix}",
        lot_id=lot.id,
        current_location_id=origin.id,
        status="available",
        effective_expires_at=expires_at,
    )
    session.add(unit)
    session.commit()

    return ColdChainScenario(unit, origin, destination)


@pytest.mark.anyio
async def test_relocates_exposed_refrigerated_unit_and_records_custody(
    db_session: Session,
) -> None:
    scenario = create_cold_chain_scenario(db_session)
    client = FakeMovementClient()

    results = await ColdChainService(client, db_session).relocate_exposed_units()

    db_session.refresh(scenario.unit)
    event = db_session.scalar(
        select(CustodyEvent).where(CustodyEvent.unit_id == scenario.unit.id)
    )
    assert scenario.destination is not None
    assert len(results) == 1
    assert results[0].moved is True
    assert client.calls == [(scenario.unit.external_unit_id, scenario.destination.code)]
    assert scenario.unit.current_location_id == scenario.destination.id
    assert event is not None
    assert event.event_type is CustodyEventType.MOVED
    assert event.from_location_id == scenario.origin.id
    assert event.to_location_id == scenario.destination.id


@pytest.mark.anyio
async def test_ignores_product_that_does_not_require_refrigeration(
    db_session: Session,
) -> None:
    create_cold_chain_scenario(db_session, requires_refrigeration=False)
    client = FakeMovementClient()

    results = await ColdChainService(client, db_session).relocate_exposed_units()

    assert results == []
    assert client.calls == []


@pytest.mark.anyio
async def test_fails_clearly_when_no_refrigerated_location_exists(
    db_session: Session,
) -> None:
    create_cold_chain_scenario(
        db_session,
        include_refrigerated_destination=False,
    )
    client = FakeMovementClient()

    with pytest.raises(NoRefrigeratedLocationError):
        await ColdChainService(client, db_session).relocate_exposed_units()

    assert client.calls == []


@pytest.mark.anyio
async def test_external_failure_keeps_exposed_unit_in_original_location(
    db_session: Session,
) -> None:
    scenario = create_cold_chain_scenario(db_session)
    client = FakeMovementClient(FarmaCentralHTTPError(409))

    with pytest.raises(FarmaCentralHTTPError):
        await ColdChainService(client, db_session).relocate_exposed_units()

    db_session.refresh(scenario.unit)
    events = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.unit_id == scenario.unit.id)
    ).all()
    assert scenario.unit.current_location_id == scenario.origin.id
    assert events == []


@pytest.mark.anyio
async def test_tries_next_refrigerated_location_after_capacity_conflict(
    db_session: Session,
) -> None:
    scenario = create_cold_chain_scenario(db_session)
    assert scenario.destination is not None
    fallback = Location(
        code=f"zz-fallback-{uuid4().hex}",
        name="Fallback cold storage",
        is_refrigerated=True,
    )
    db_session.add(fallback)
    db_session.commit()
    client = FakeMovementClient(
        errors_by_store={
            scenario.destination.code: FarmaCentralHTTPError(409),
        }
    )

    results = await ColdChainService(client, db_session).relocate_exposed_units()

    db_session.refresh(scenario.unit)
    events = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.unit_id == scenario.unit.id)
    ).all()
    assert len(results) == 1
    assert client.calls == [
        (scenario.unit.external_unit_id, scenario.destination.code),
        (scenario.unit.external_unit_id, fallback.code),
    ]
    assert scenario.unit.current_location_id == fallback.id
    assert len(events) == 1
    assert events[0].event_type is CustodyEventType.MOVED
    assert events[0].to_location_id == fallback.id
