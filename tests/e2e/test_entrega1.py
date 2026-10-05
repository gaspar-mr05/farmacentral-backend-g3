"""Entrega 1 completa con PostgreSQL real y APIs externas simuladas."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.api.dependencies import get_farma_central_client
from app.main import app
from app.models import CustodyEvent, CustodyEventType, Order, Unit
from app.services.inventory.movements import ProductMovementService
from app.services.inventory.sync import InventorySyncService
from app.services.production.orchestration import produce
from tests.api.test_orders_payments import (
    FakeCheckoutClient,
    _override_checkout,
    _override_market_prices,
)


class Entrega1Client:
    """Inventario externo que consume insumos y entrega la producción al instante."""

    def __init__(self):
        suffix = uuid4().hex
        self.raw = f"RAW-{suffix}"
        self.intermediate = f"INTERMEDIATE-{suffix}"
        self.kit = f"KIT-{suffix}"
        self.reception = f"IN-{suffix}"
        self.packaging = f"PACKAGING-{suffix}"
        self.warehouse = f"WAREHOUSE-{suffix}"
        self.dispatch = f"OUT-{suffix}"
        self.expires_at = datetime.now(UTC) + timedelta(days=30)
        self.units = {
            self.raw: {
                "_id": f"UNIT-{self.raw}",
                "sku": self.raw,
                "store": self.reception,
                "batch": f"LOT-{self.raw}",
                "expiresAt": self.expires_at.isoformat(),
            }
        }
        self.components = {self.intermediate: self.raw, self.kit: self.intermediate}

    async def get_available_products(self):
        return [
            {
                "sku": sku,
                "name": sku,
                "production": {
                    "batch": 1,
                    "at": "farma-central" if sku == self.raw else "packaging",
                },
                "sellable": sku == self.kit,
                "components": (
                    [{"sku": self.components[sku], "req": 1}]
                    if sku in self.components
                    else []
                ),
            }
            for sku in (self.raw, self.intermediate, self.kit)
        ]

    async def get_spaces(self):
        return [
            {"_id": self.reception, "checkIn": True},
            {"_id": self.packaging, "packaging": True},
            {"_id": self.warehouse},
            {"_id": self.dispatch, "checkOut": True},
        ]

    async def get_space_inventory(self, store_id):
        return [
            {"sku": unit["sku"], "quantity": 1}
            for unit in self.units.values()
            if unit["store"] == store_id
        ]

    async def get_space_products(self, store_id, sku, *, limit=None):
        return [
            unit.copy()
            for unit in self.units.values()
            if unit["store"] == store_id and unit["sku"] == sku
        ]

    async def move_product(self, product_id, store_id):
        unit = next(u for u in self.units.values() if u["_id"] == product_id)
        unit["store"] = store_id

    async def request_fabrication_challenge(self, sku, quantity):
        return {
            "challengeId": f"challenge-{sku}",
            "prefix": "e1",
            "algorithm": "sha256-leading-zero-bits",
            "difficulty": 0,
            "sku": sku,
            "quantity": quantity,
            "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }

    async def request_products(self, *, sku, quantity, challenge_id, nonce):
        del self.units[self.components[sku]]
        self.units[sku] = {
            "_id": f"UNIT-{sku}",
            "sku": sku,
            "store": self.packaging,
            "expiresAt": self.expires_at.isoformat(),
        }
        return {
            "sku": sku,
            "group": 3,
            "quantity": quantity,
            "availableAt": datetime.now(UTC).isoformat(),
        }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("payment_status", "order_status"),
    [("SUCCESS", "dispatched"), ("CANCELLED", "cancelled"), ("ERROR", "payment_error")],
)
async def test_entrega1_from_raw_material_to_customer(
    api_client, db_session, payment_status, order_status
):
    client = Entrega1Client()
    app.dependency_overrides[get_farma_central_client] = lambda: client
    sync = InventorySyncService(client, db_session)
    movements = ProductMovementService(client, db_session)

    # Partimos con un insumo disponible: sync registra recepción y luego movimiento.
    await sync.synchronize()
    raw = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == f"UNIT-{client.raw}")
    )
    raw_lot_id = raw.lot_id
    await movements.move(
        product_id=raw.external_unit_id, destination_store=client.packaging
    )
    for sku in (client.intermediate, client.kit):
        run, _ = await produce(db_session, client=client, sku=sku, quantity=1)
        # PostgreSQL now() conserva la hora de la transacción externa del fixture.
        run.requested_at = raw.created_at
        db_session.flush()
        await sync.synchronize()
        db_session.refresh(run)
        assert run.completed_at is not None
        assert len(run.output_lot.units) == 1

    kit = db_session.scalar(
        select(Unit).where(Unit.external_unit_id == f"UNIT-{client.kit}")
    )
    await movements.move(
        product_id=kit.external_unit_id, destination_store=client.warehouse
    )
    _override_market_prices(db_session, {client.kit: 3000})
    _override_checkout(FakeCheckoutClient(external_status=payment_status))
    catalog = api_client.get("/api/catalog")
    assert catalog.status_code == 200
    item = next(item for item in catalog.json() if item["sku"] == client.kit)
    assert item["stock"] == 1
    assert item["price"] == 3000

    response = api_client.post(
        "/api/orders",
        json={
            "buyer_name": "Ada Lovelace",
            "buyer_email": "ada@example.com",
            "items": [{"sku": client.kit, "quantity": 1}],
        },
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert response.json()["total"] == 3000
    payment = api_client.post(f"/api/orders/{order_id}/payments")
    assert payment.status_code == 201
    callback = api_client.get(
        f"/api/payments/{payment.json()['id']}/return/{payment_status.lower()}"
    )
    assert callback.status_code == 200
    assert callback.json()["status"] == payment_status.lower()

    if payment_status == "SUCCESS":
        completed = api_client.get(f"/api/orders/{order_id}")
        assert completed.status_code == 200
        assert completed.json()["status"] == order_status
        assigned = completed.json()["items"][0]["assigned_units"]
        assert len(assigned) == 1
        assert assigned[0]["external_unit_id"] == kit.external_unit_id
        assert assigned[0]["dispatched_at"] is not None
        assert client.units[client.kit]["store"] == client.dispatch
        updated_catalog = api_client.get("/api/catalog")
        updated_item = next(
            item for item in updated_catalog.json() if item["sku"] == client.kit
        )
        assert updated_item["stock"] == 0
    else:
        fulfillment = api_client.post(f"/api/orders/{order_id}/fulfillment")
        dispatch = api_client.post(f"/api/orders/{order_id}/dispatch")
        assert fulfillment.status_code == dispatch.status_code == 409
        assert kit.status == "available"
        assert client.units[client.kit]["store"] == client.warehouse

    db_session.expire_all()
    assert db_session.get(Order, UUID(order_id)).status.value == order_status

    trace = api_client.get(f"/api/traceability/{kit.lot_id}")
    assert trace.status_code == 200
    assert {lot["product_sku"] for lot in trace.json()["ancestors"]} == {
        client.raw,
        client.intermediate,
    }
    upstream = api_client.get(f"/api/traceability/{raw_lot_id}")
    assert upstream.status_code == 200
    assert {lot["product_sku"] for lot in upstream.json()["descendants"]} == {
        client.intermediate,
        client.kit,
    }
    deliveries = upstream.json()["deliveries"]
    if payment_status == "SUCCESS":
        assert len(deliveries) == 1
        assert deliveries[0]["order_id"] == order_id
        assert deliveries[0]["buyer_email"] == "ada@example.com"
        assert deliveries[0]["lot_id"] == str(kit.lot_id)
    else:
        assert deliveries == []
    events = set(
        db_session.scalars(
            select(CustodyEvent.event_type).where(CustodyEvent.unit_id == raw.id)
        )
    )
    assert {
        CustodyEventType.RECEIVED,
        CustodyEventType.MOVED,
        CustodyEventType.PRODUCTION_CONSUMED,
    } <= events
