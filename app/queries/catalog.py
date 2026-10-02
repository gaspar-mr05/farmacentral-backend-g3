from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import Location, Lot, Product, ProductCategory, Unit


@dataclass(frozen=True)
class CatalogStock:
    sku: str
    name: str
    stock: int


def list_sellable_catalog(
    session: Session,
    *,
    as_of: datetime,
) -> list[CatalogStock]:
    statement = (
        select(
            Product.sku,
            Product.name,
            func.count(Location.id).label("stock"),
        )
        .select_from(Product)
        .outerjoin(Lot, Lot.product_id == Product.id)
        .outerjoin(
            Unit,
            and_(
                Unit.lot_id == Lot.id,
                Unit.status == "available",
                Unit.effective_expires_at > as_of,
            ),
        )
        .outerjoin(
            Location,
            and_(
                Location.id == Unit.current_location_id,
                Location.is_sellable.is_(True),
            ),
        )
        .where(Product.category == ProductCategory.KIT)
        .group_by(Product.id, Product.sku, Product.name)
        .order_by(Product.name, Product.sku)
    )

    return [
        CatalogStock(
            sku=row.sku,
            name=row.name,
            stock=row.stock,
        )
        for row in session.execute(statement)
    ]
