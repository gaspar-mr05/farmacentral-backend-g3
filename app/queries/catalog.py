from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import Location, Lot, OrderUnit, Product, ProductCategory, Unit


@dataclass(frozen=True)
class CatalogLotStock:
    external_lot_id: str
    stock: int
    next_expiry_at: datetime


@dataclass(frozen=True)
class CatalogStock:
    sku: str
    name: str
    lots: tuple[CatalogLotStock, ...]

    @property
    def stock(self) -> int:
        return sum(lot.stock for lot in self.lots)

    @property
    def next_expiry_at(self) -> datetime | None:
        return self.lots[0].next_expiry_at if self.lots else None


def list_sellable_catalog(
    session: Session,
    *,
    as_of: datetime,
) -> list[CatalogStock]:
    products = session.execute(
        select(Product.sku, Product.name)
        .where(Product.category == ProductCategory.KIT)
        .order_by(Product.name, Product.sku)
    ).all()
    lot_statement = (
        select(
            Product.sku,
            Lot.external_lot_id,
            func.count(Unit.id).label("stock"),
            func.min(Unit.effective_expires_at).label("next_expiry_at"),
        )
        .join(Lot, Lot.product_id == Product.id)
        .join(Unit, Unit.lot_id == Lot.id)
        .join(Location, Location.id == Unit.current_location_id)
        .where(
            Product.category == ProductCategory.KIT,
            Unit.status == "available",
            Unit.effective_expires_at > as_of,
            ~exists().where(OrderUnit.unit_id == Unit.id),
            Location.is_sellable.is_(True),
        )
        .group_by(Product.sku, Lot.id, Lot.external_lot_id)
        .order_by(Product.sku, func.min(Unit.effective_expires_at), Lot.external_lot_id)
    )
    lots_by_sku: dict[str, list[CatalogLotStock]] = defaultdict(list)
    for row in session.execute(lot_statement):
        lots_by_sku[row.sku].append(
            CatalogLotStock(
                external_lot_id=row.external_lot_id,
                stock=row.stock,
                next_expiry_at=row.next_expiry_at,
            )
        )

    return [
        CatalogStock(
            sku=row.sku,
            name=row.name,
            lots=tuple(lots_by_sku[row.sku]),
        )
        for row in products
    ]
