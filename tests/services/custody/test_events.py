from sqlalchemy.orm import Session

from app.models import (
    CustodyEvent,
    CustodyEventType,
    LotOrigin,
    ProductCategory,
    Unit,
)
from app.schemas.inventory import InventoryData
from app.schemas.locations import LocationData
from app.schemas.lots import LotData
from app.schemas.products import ProductData
from app.schemas.units import UnitData
from app.services.inventory.sync import sync_inventory


def _build_inventory_data(
    unit_location: str = "RECEPCION",
    external_unit_id: str = "UNIT-001",
) -> InventoryData:
    """Helper para construir un objeto InventoryData válido en las pruebas."""
    return InventoryData(
        products=[
            ProductData(
                sku="PROD-001",
                name="Paracetamol",
                category=ProductCategory.INSUMO,
                batch_size=100,
                requires_refrigeration=False,
            )
        ],
        locations=[
            LocationData(
                code=unit_location,
                name=f"Bodega {unit_location}",
                is_refrigerated=False,
            )
        ],
        lots=[
            LotData(
                external_lot_id="LOT-001",
                product_sku="PROD-001",
                expires_at=None,
                origin=LotOrigin.FARMA_CENTRAL,
            )
        ],
        units=[
            UnitData(
                external_unit_id=external_unit_id,
                lot_external_id="LOT-001",
                location_code=unit_location,
                status="available",
            )
        ],
    )


def test_sync_creates_received_event_for_new_unit(db_session: Session) -> None:
    data = _build_inventory_data(unit_location="RECEPCION")

    sync_inventory(db_session, data)

    unit = db_session.query(Unit).one()
    events = db_session.query(CustodyEvent).filter_by(unit_id=unit.id).all()

    assert len(events) == 1
    assert events[0].event_type == CustodyEventType.RECEIVED
    assert events[0].from_location_id is None
    assert events[0].to_location_id == unit.current_location_id


def test_sync_creates_moved_event_when_location_changes(db_session: Session) -> None:
    data_v1 = _build_inventory_data(unit_location="RECEPCION")
    sync_inventory(db_session, data_v1)

    data_v2 = _build_inventory_data(unit_location="CAMARA_FRIO")
    sync_inventory(db_session, data_v2)

    unit = db_session.query(Unit).one()
    events = (
        db_session.query(CustodyEvent)
        .filter_by(unit_id=unit.id)
        .order_by(CustodyEvent.occurred_at)
        .all()
    )

    assert len(events) == 2
    assert events[0].event_type == CustodyEventType.RECEIVED
    assert events[1].event_type == CustodyEventType.MOVED
    assert events[1].from_location_id != events[1].to_location_id


def test_sync_does_not_duplicate_events_when_nothing_changes(
    db_session: Session,
) -> None:
    data = _build_inventory_data(unit_location="RECEPCION")

    sync_inventory(db_session, data)
    sync_inventory(db_session, data)

    unit = db_session.query(Unit).one()
    events = db_session.query(CustodyEvent).filter_by(unit_id=unit.id).all()

    assert len(events) == 1
    assert events[0].event_type == CustodyEventType.RECEIVED
