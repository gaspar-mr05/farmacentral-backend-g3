from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import (
    ExternalOrderStatus,
    Location,
    Lot,
    LotOrigin,
    Order,
    OrderItem,
    OrderSource,
    OrderStatus,
    OrderUnit,
    Product,
    ProductCategory,
    ProductionInput,
    ProductionInputUnit,
    ProductionRun,
    Recall,
    Unit,
)


class RecallInventory:
    def __init__(self, session):
        self.session = session
        self.now = datetime.now(UTC)
        self.expiry = self.now + timedelta(days=30)
        self.suffix = uuid4().hex
        self.products = {}
        self.runs = {}
        self.locations = {}
        for role in ("storage", "buffer", "reception", "quarantine"):
            location = Location(
                code=f"{role}-{self.suffix}",
                name=role,
                is_sellable=role in {"storage", "buffer"},
            )
            session.add(location)
            self.locations[role] = location
        session.flush()

    def lot(self, name, sku="KIT-RECALL"):
        if sku not in self.products:
            product = Product(
                sku=f"{sku}-{self.suffix}",
                name=sku,
                category=ProductCategory.INSUMO
                if sku == "RAW"
                else ProductCategory.KIT,
                batch_size=1,
            )
            self.session.add(product)
            self.products[sku] = product
            self.session.flush()
        lot = Lot(
            external_lot_id=f"{name}-{self.suffix}",
            product=self.products[sku],
            origin=LotOrigin.OWN_PRODUCTION,
            expires_at=self.expiry,
        )
        self.session.add(lot)
        self.session.flush()
        return lot

    def unit(self, name, lot, status="available", location="storage", *, expired=False):
        unit = Unit(
            external_unit_id=f"{name}-{self.suffix}",
            lot=lot,
            current_location=self.locations[location],
            status=status,
            effective_expires_at=self.now - timedelta(days=1)
            if expired
            else self.expiry,
        )
        self.session.add(unit)
        self.session.flush()
        return unit

    def link(self, output, *inputs, quantity=1, consumed_unit=None):
        key = output.id if output is not None else uuid4()
        if key not in self.runs:
            run = ProductionRun(
                output_lot_id=output.id if output else None,
                expected_sku=output.product.sku if output else "PENDING",
                expected_quantity=1,
                requested_at=self.now,
                completed_at=self.now if output else None,
            )
            self.session.add(run)
            self.session.flush()
            self.runs[key] = run
        for lot in inputs:
            record = ProductionInput(
                production_run_id=self.runs[key].id,
                input_lot_id=lot.id,
                quantity_consumed=quantity,
            )
            self.session.add(record)
            self.session.flush()
            if consumed_unit is not None:
                self.session.add(
                    ProductionInputUnit(
                        production_input_id=record.id, unit_id=consumed_unit.id
                    )
                )
                self.session.flush()

    def order(self, *deliveries, external=True):
        order = Order(
            buyer_name="Farmacia",
            buyer_email=None if external else "cliente@example.test",
            external_order_id=uuid4().hex[:24] if external else None,
            external_customer_id="farmacia-externa" if external else None,
            source=OrderSource.SFTP if external else OrderSource.WEB,
            status=OrderStatus.ACCEPTED if external else OrderStatus.PAID,
            external_status=ExternalOrderStatus.ACCEPTED if external else None,
            total=1500 * len(deliveries),
        )
        self.session.add(order)
        for unit, dispatched in deliveries:
            item = OrderItem(sku=unit.lot.product.sku, quantity=1, unit_price=1500)
            order.items.append(item)
            item.assigned_units.append(
                OrderUnit(unit=unit, dispatched_at=self.now if dispatched else None)
            )
            unit.status = "dispatched" if dispatched else "reserved"
        self.session.flush()
        return order

    def recall(self, origin):
        recall = Recall(
            recall_id=f"rcl-{self.suffix}",
            batch=origin.external_lot_id,
            sku=origin.product.sku,
            reason="Calidad",
            issued_at=self.now,
            deadline=self.now + timedelta(minutes=30),
        )
        self.session.add(recall)
        self.session.flush()
        return recall
