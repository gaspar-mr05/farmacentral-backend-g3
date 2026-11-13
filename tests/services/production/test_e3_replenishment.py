from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralHTTPError, FarmaCentralTimeoutError
from app.models import (
    BillingSyncState,
    BusinessPeriod,
    Lot,
    MarketOffer,
    MarketOfferReservation,
    OrderItem,
    OrderStatus,
    ProductionRun,
    ReplenishmentState,
    Unit,
)
from app.schemas.procurement import ProcurementDemand
from app.schemas.purchase_orders import PurchaseOrder
from app.schemas.trades import TradeOffer
from app.services.inventory.sync import InventorySyncService
from app.services.orders.processing import OrderProcessingService
from app.services.production.locking import production_lock
from app.services.production.replenishment import E3ReplenishmentService
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.services.test_procurement_planning import product
from tests.support.farma_central import make_settings
from tests.support.orders import make_external_order


class Pharmacy:
    def __init__(self, *, restricted=False):
        self.raw = "SOB-PAPEL-EST" if restricted else "RAW"
        self.catalog = {
            self.raw: product(self.raw, cost=1, time=0),
            "MID": product("MID", components=[(self.raw, 1)], time=0),
            "KIT-A": product("KIT-A", components=[("MID", 1)], time=0),
        }
        self.spaces = [
            {"_id": name, "totalSpace": 200, "usedSpace": 0, **flags}
            for name, flags in [
                ("RECEIVE", {"checkIn": True}),
                ("STORE", {}),
                ("BUFFER", {"buffer": True}),
                ("PACK", {"packaging": True}),
            ]
        ]
        self.units = {}
        self.posts = []
        self.lose_fabrication_response = False

    async def get_available_products(self):
        return [p.model_dump(by_alias=True) for p in self.catalog.values()]

    async def get_spaces(self):
        return [
            s | {"usedSpace": sum(u["store"] == s["_id"] for u in self.units.values())}
            for s in self.spaces
        ]

    async def get_space_inventory(self, space):
        return [
            {
                "sku": sku,
                "quantity": sum(
                    u["sku"] == sku and u["store"] == space for u in self.units.values()
                ),
            }
            for sku in self.catalog
        ]

    async def get_space_products(self, space, sku, *, limit=None):
        return [
            u.copy()
            for u in self.units.values()
            if u["sku"] == sku and u["store"] == space
        ][:limit]

    async def move_product(self, product_id, store_id):
        self.units[product_id]["store"] = store_id

    async def request_fabrication_challenge(self, sku, quantity):
        return {
            "challengeId": "challenge",
            "prefix": "test",
            "algorithm": "sha256-leading-zero-bits",
            "difficulty": 0,
            "sku": sku,
            "quantity": quantity,
            "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }

    def receive(self, sku, quantity, *, batch=None, store="RECEIVE"):
        for _ in range(quantity):
            uid = uuid4().hex[:24]
            self.units[uid] = {
                "_id": uid,
                "sku": sku,
                "batch": batch or f"L-{sku}-{len(self.posts)}",
                "store": store,
                "expiresAt": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            }

    async def request_products(self, *, sku, quantity, **kwargs):
        self.posts.append((sku, quantity))
        recipe = self.catalog[sku]
        for comp in recipe.components:
            matches = [
                uid
                for uid, u in self.units.items()
                if u["sku"] == comp.sku and u["store"] == "PACK"
            ]
            assert len(matches) >= comp.required_per_unit * quantity
            for uid in matches[: comp.required_per_unit * quantity]:
                del self.units[uid]
        self.receive(sku, quantity, store="PACK" if recipe.components else "RECEIVE")
        if recipe.components and self.lose_fabrication_response:
            raise FarmaCentralTimeoutError("response lost")
        return {
            "sku": sku,
            "quantity": quantity,
            "group": 3,
            "availableAt": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }


def setup(session, pharmacy, *, trades=None, orders=None):
    period = BusinessPeriod(
        environment="dev",
        code=uuid4().hex,
        group_id=3,
        starts_at=datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(period)
    session.commit()
    trades = trades or Mock(
        list_offers=AsyncMock(return_value=[]),
        create_offer=AsyncMock(),
        get_offer=AsyncMock(),
    )
    prices = Mock(
        get_current_prices=AsyncMock(
            return_value=[
                {"sku": sku, "price": 10 if sku.startswith("KIT") else 1}
                for sku in pharmacy.catalog
            ]
        )
    )
    source = E3ReplenishmentService(
        pharmacy,
        trades,
        orders or Mock(),
        prices,
        session,
        period,
        settings=make_settings(
            replenishment_minimum_stock=1, replenishment_target_stock=1
        ),
    )
    return source


def next_turn(session):
    for state in session.scalars(select(ReplenishmentState)):
        state.next_retry_at = None
    session.commit()


@pytest.mark.anyio
async def test_initial_zero_accounting_balance_allows_budgeted_startup(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        session.add(
            BillingSyncState(
                period_id=service.period.id,
                stream="bank",
                balance=0,
                last_success_at=datetime.now(UTC),
            )
        )
        session.commit()
        result = await service.poll()
        assert result["supplied"] == 1 and result["failed"] == 0
        assert result["kits"][0]["spending_room"] == "10"
        assert pharmacy.posts == [("RAW", 1)]


@pytest.mark.anyio
async def test_multilevel_production_resumes_without_waiting_and_preserves_lineage(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        totals = []
        for _ in range(6):
            totals.append(await service.poll())
            next_turn(session)
        assert pharmacy.posts == [("RAW", 1), ("MID", 1), ("KIT-A", 1)], totals
        assert sum(r["produced"] for r in totals) == 2
        kit = session.scalar(select(Lot).where(Lot.product.has(sku="KIT-A")))
        assert kit.produced_by.inputs[0].input_lot.product.sku == "MID"
        assert (
            kit.produced_by.inputs[0]
            .input_lot.produced_by.inputs[0]
            .input_lot.product.sku
            == "RAW"
        )
        assert session.scalar(
            select(Unit).where(Unit.lot_id == kit.id)
        ).current_location.is_sellable
        state = session.scalar(select(ReplenishmentState))
        assert state.pending_operation is None
        assert state.estimated_spend == 3
        assert state.minimum == 1
        # Each turn released packaging; another process can use it.
        with Session(migration_database) as other, production_lock(other):
            pass


@pytest.mark.anyio
async def test_restricted_input_publishes_one_bid_without_direct_request(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy(restricted=True)
    now = datetime.now(UTC)
    bid = TradeOffer(
        id="a" * 24,
        state="open",
        side="buy",
        sku=pharmacy.raw,
        quantity=1,
        unitPrice=1,
        publisher="3",
        createdAt=now,
        expiresAt=now + timedelta(hours=1),
    )
    trades = Mock(
        list_offers=AsyncMock(return_value=[]),
        create_offer=AsyncMock(return_value=bid),
        get_offer=AsyncMock(return_value=bid),
    )
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy, trades=trades)
        await service.poll()
        next_turn(session)
        await service.poll()
        assert pharmacy.posts == []
        trades.create_offer.assert_awaited_once()
        assert session.scalar(select(ProductionRun)) is None
        assert session.scalar(select(ReplenishmentState)).procurement_id is not None
        with Session(migration_database) as other, production_lock(other):
            pass


@pytest.mark.anyio
async def test_market_reservation_and_component_runs_are_not_final_kit_capacity(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    pharmacy.receive("KIT-A", 1, store="STORE")
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        await InventorySyncService(pharmacy, session).synchronize(tolerate_changes=True)
        unit = session.scalar(select(Unit))
        offer = MarketOffer(
            period_id=service.period.id,
            side="sell",
            publisher_group_id=3,
            sku="KIT-A",
            quantity=1,
            unit_price=10,
            status="open",
        )
        session.add(offer)
        session.flush()
        session.add(
            MarketOfferReservation(
                offer_id=offer.id,
                unit_id=unit.id,
                reserved_at=datetime.now(UTC),
            )
        )
        now = datetime.now(UTC)
        session.add(
            ProductionRun(
                expected_sku="MID",
                expected_quantity=20,
                requested_at=now,
                available_at=now + timedelta(minutes=5),
            )
        )
        session.commit()
        decisions = OrderProcessingService(Mock(), session)
        deadline = now + timedelta(hours=1)
        assert decisions._available_stock("KIT-A", deadline) == 0
        assert decisions._confirmed_production("KIT-A", now, deadline) == 0


@pytest.mark.anyio
async def test_lost_fabrication_response_is_not_repeated(migration_database):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    pharmacy.lose_fabrication_response = True
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        first = await service.poll()
        next_turn(session)
        second = await service.poll()
        state = session.scalar(select(ReplenishmentState))
        assert state.pending_operation is not None, (first, second)
        assert state.pending_operation["status"] == "uncertain"
        for _ in range(2):
            next_turn(session)
            await service.poll()
        assert pharmacy.posts == [("RAW", 1), ("MID", 1)]


@pytest.mark.anyio
async def test_purchase_of_finished_kit_uses_original_batch_without_fake_manufacture(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy(restricted=True)
    now = datetime.now(UTC)
    opened = TradeOffer(
        id="c" * 24,
        state="open",
        side="sell",
        sku="KIT-A",
        quantity=1,
        unitPrice=8,
        publisher="5",
        createdAt=now,
        expiresAt=now + timedelta(hours=1),
    )
    closed = opened.model_copy(
        update={
            "status": "closed",
            "order_id": "d" * 24,
            "counterparty_group": 3,
            "closed_at": now,
        }
    )
    trades = Mock(
        list_offers=AsyncMock(
            side_effect=lambda **kw: [opened] if kw["sku"] == "KIT-A" else []
        ),
        get_offer=AsyncMock(side_effect=[opened, closed]),
    )

    async def accept(_):
        pharmacy.receive("KIT-A", 1, batch="L-FOREIGN-KIT")
        return closed

    trades.accept_offer = AsyncMock(side_effect=accept)
    order = PurchaseOrder.model_validate(
        {
            "id": "d" * 24,
            "cliente": "3",
            "proveedor": "5",
            "sku": "KIT-A",
            "cantidad": 1,
            "despachado": 1,
            "precioUnitario": 8,
            "estado": "cumplida",
            "historial": [],
            "creada": now,
            "actualizada": now,
            "vencimiento": now + timedelta(hours=1),
            "facturado": False,
        }
    )
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(
            session,
            pharmacy,
            trades=trades,
            orders=Mock(get_order=AsyncMock(return_value=order)),
        )
        assert (await service.poll())["purchased"] == 1
        next_turn(session)
        assert (await service.poll())["ready"] == 1
        assert pharmacy.posts == []
        lot = session.scalar(select(Lot).where(Lot.external_lot_id == "L-FOREIGN-KIT"))
        assert lot.produced_by is None
        assert session.scalar(
            select(Unit).where(Unit.lot_id == lot.id)
        ).current_location.is_sellable


@pytest.mark.anyio
async def test_partial_component_stock_is_completed_instead_of_restarting_recipe(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    pharmacy.receive("MID", 1, batch="L-PURCHASED-MID", store="STORE")
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        session.add(
            make_external_order(
                status=OrderStatus.ACCEPTED,
                items=[OrderItem(sku="KIT-A", quantity=2, unit_price=10)],
            )
        )
        session.commit()
        totals = []
        for _ in range(5):
            result = await service.poll()
            totals.append(result)
            assert result["failed"] == 0, result
            next_turn(session)
        assert pharmacy.posts == [
            ("RAW", 1),
            ("MID", 1),
            ("KIT-A", 2),
        ], totals


@pytest.mark.anyio
async def test_accepted_demand_precedes_waiting_orders_and_background_buffers(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    for sku in ("KIT-B", "KIT-C"):
        pharmacy.catalog[sku] = product(sku, components=[("RAW", 1)], time=0)
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        for index, (sku, status) in enumerate(
            [("KIT-B", OrderStatus.RECEIVED), ("KIT-C", OrderStatus.ACCEPTED)]
        ):
            session.add(
                make_external_order(
                    external_order_id=f"{index:024x}",
                    status=status,
                    items=[OrderItem(sku=sku, quantity=1, unit_price=10)],
                )
            )
        session.commit()
        first = await service.poll()
        assert first["kits"][0]["sku"] == "KIT-C"
        next_turn(session)
        second = await service.poll()
        assert second["kits"][0]["sku"] == "KIT-C"
        assert pharmacy.posts == [("RAW", 1), ("KIT-C", 1)]
        # Waiting acquisition must not consume the higher priority kit's input.
        assert first["supplied"] == 1 and second["produced"] == 1


@pytest.mark.anyio
async def test_rate_limit_persists_cooldown_without_repeating_supply(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    pharmacy.request_products = AsyncMock(
        side_effect=FarmaCentralHTTPError(
            429, "private response", retry_after_seconds=180
        )
    )
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        result = await service.poll()
        state = session.scalar(select(ReplenishmentState))
        assert result["failed"] == 1
        assert state.http_status == 429
        assert state.pending_operation is None
        assert state.next_retry_at >= datetime.now(UTC) + timedelta(seconds=175)
        assert "private" not in state.last_error
        restarted = E3ReplenishmentService(
            pharmacy,
            service.sourcing.trades,
            Mock(),
            service.prices,
            session,
            service.period,
            settings=service.settings,
        )
        assert (await restarted.poll())["deferred"] == 1
        pharmacy.request_products.assert_awaited_once()


@pytest.mark.anyio
async def test_shared_inputs_are_protected_for_earlier_accepted_commitments(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    pharmacy.catalog["KIT-B"] = product("KIT-B", components=[("RAW", 1)])
    pharmacy.receive("RAW", 1, store="STORE")
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        session.add(
            make_external_order(
                status=OrderStatus.ACCEPTED,
                items=[OrderItem(sku="KIT-A", quantity=1, unit_price=10)],
            )
        )
        session.commit()
        await InventorySyncService(pharmacy, session).synchronize(tolerate_changes=True)
        plan = await service.sourcing.preview(
            ProcurementDemand(
                sku="KIT-B",
                quantity=1,
                expected_revenue=10,
                deadline=datetime.now(UTC) + timedelta(hours=3),
                includes_committed=True,
                priority=2,
            )
        )
        assert plan.protected_stock["RAW"] == 1
        assert any(a.sku == "RAW" and a.kind == "direct" for a in plan.actions)
        assert pharmacy.posts == []


@pytest.mark.anyio
async def test_stock_expiring_before_customer_deadline_does_not_skip_replenishment(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    pharmacy.receive("KIT-A", 1, store="STORE")
    for unit in pharmacy.units.values():
        unit["expiresAt"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        session.add(
            make_external_order(
                status=OrderStatus.ACCEPTED,
                items=[OrderItem(sku="KIT-A", quantity=1, unit_price=10)],
            )
        )
        session.commit()
        result = await service.poll()
        assert result["supplied"] == 1 and result["ready"] == 0
        assert pharmacy.posts == [("RAW", 1)]


@pytest.mark.anyio
async def test_completed_cycle_does_not_reset_period_spending_limit(migration_database):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        service.settings.replenishment_spending_limit = 3
        for _ in range(5):
            assert (await service.poll())["failed"] == 0
            next_turn(session)
        for key in list(pharmacy.units):
            if pharmacy.units[key]["sku"] == "KIT-A":
                del pharmacy.units[key]
        blocked = await service.poll()
        state = session.scalar(select(ReplenishmentState))
        assert state.estimated_spend == 3
        assert blocked["produced"] == blocked["supplied"] == blocked["purchased"] == 0
        assert blocked["kits"][0]["spending_room"] == "0"
        assert pharmacy.posts == [("RAW", 1), ("MID", 1), ("KIT-A", 1)]


@pytest.mark.anyio
async def test_useful_batch_production_pays_full_lots_but_allocates_consumption(
    migration_database,
):
    run_migration(migration_database, "upgrade", "head")
    pharmacy = Pharmacy()
    pharmacy.catalog["RAW"].production.batch = 10
    pharmacy.catalog["MID"].production.batch = 4
    with Session(migration_database, expire_on_commit=False) as session:
        service = setup(session, pharmacy)
        service.settings.replenishment_spending_limit = 20
        service.prices.get_current_prices.return_value = [
            {"sku": sku, "price": 20 if sku == "KIT-A" else 1}
            for sku in pharmacy.catalog
        ]
        session.add(
            make_external_order(
                status=OrderStatus.ACCEPTED,
                items=[OrderItem(sku="KIT-A", quantity=1, unit_price=10)],
            )
        )
        session.commit()
        for _ in range(5):
            result = await service.poll()
            assert result["failed"] == 0, result
            next_turn(session)
        assert pharmacy.posts == [("RAW", 10), ("MID", 4), ("KIT-A", 1)]
        assert session.scalar(select(ReplenishmentState)).estimated_spend == 15
        assert sum(u["sku"] == "MID" for u in pharmacy.units.values()) == 3
