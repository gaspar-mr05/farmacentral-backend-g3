import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central_exceptions import FarmaCentralHTTPError
from app.models import CustodyEvent, CustodyEventType, Unit
from app.services.inventory.movements import ProductMovementService
from tests.support.inventory import create_movement_scenario


class FakeMovementClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def move_product(self, product_id: str, store_id: str) -> None:
        self.calls.append((product_id, store_id))
        if self.error is not None:
            raise self.error


@pytest.mark.anyio
async def test_move_updates_location_and_records_custody_event(
    db_session: Session,
) -> None:
    scenario = create_movement_scenario(db_session)
    client = FakeMovementClient()

    result = await ProductMovementService(client, db_session).move(
        product_id=scenario.product_id,
        destination_store=scenario.destination_code,
    )

    unit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == scenario.product_id)
    )
    event = db_session.scalar(
        select(CustodyEvent).where(CustodyEvent.unit_id == scenario.unit.id)
    )
    assert result.moved is True
    assert result.from_store == scenario.origin_code
    assert result.to_store == scenario.destination_code
    assert client.calls == [(scenario.product_id, scenario.destination_code)]
    assert unit is not None
    assert unit.current_location_id == scenario.destination.id
    assert event is not None
    assert event.event_type is CustodyEventType.MOVED
    assert event.from_location_id == scenario.origin.id
    assert event.to_location_id == scenario.destination.id


@pytest.mark.anyio
async def test_external_failure_keeps_local_state_unchanged(
    db_session: Session,
) -> None:
    scenario = create_movement_scenario(db_session)
    client = FakeMovementClient(FarmaCentralHTTPError(409))

    with pytest.raises(FarmaCentralHTTPError):
        await ProductMovementService(client, db_session).move(
            product_id=scenario.product_id,
            destination_store=scenario.destination_code,
        )

    unit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == scenario.product_id)
    )
    events = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.unit_id == scenario.unit.id)
    ).all()
    assert unit is not None
    assert unit.current_location_id == scenario.origin.id
    assert events == []


@pytest.mark.anyio
async def test_move_to_current_location_is_an_idempotent_noop(
    db_session: Session,
) -> None:
    scenario = create_movement_scenario(db_session)
    client = FakeMovementClient()

    result = await ProductMovementService(client, db_session).move(
        product_id=scenario.product_id,
        destination_store=scenario.origin_code,
    )

    events = db_session.scalars(
        select(CustodyEvent).where(CustodyEvent.unit_id == scenario.unit.id)
    ).all()
    assert result.moved is False
    assert client.calls == []
    assert events == []
