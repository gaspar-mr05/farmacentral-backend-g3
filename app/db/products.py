from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.schemas.products import ProductData


@dataclass(frozen=True)
class ProductUpsertResult:
    created: int
    updated: int
    by_sku: dict[str, Product]


def get_product_by_sku(session: Session, sku: str) -> Product | None:
    return session.scalar(select(Product).where(Product.sku == sku))


def upsert_products(
    session: Session, records: Iterable[ProductData]
) -> ProductUpsertResult:
    products = {product.sku: product for product in session.scalars(select(Product))}
    created = updated = 0

    for record in records:
        product = products.get(record.sku)
        if product is None:
            product = Product(
                sku=record.sku,
                name=record.name,
                category=record.category,
                batch_size=record.batch_size,
                requires_refrigeration=record.requires_refrigeration,
            )
            session.add(product)
            products[record.sku] = product
            created += 1
            continue

        changed = (
            product.name != record.name
            or product.category != record.category
            or product.batch_size != record.batch_size
            or product.requires_refrigeration != record.requires_refrigeration
        )
        product.name = record.name
        product.category = record.category
        product.batch_size = record.batch_size
        product.requires_refrigeration = record.requires_refrigeration
        updated += int(changed)

    return ProductUpsertResult(created, updated, products)


def list_products(session: Session) -> list[Product]:
    statement = select(Product).order_by(Product.name, Product.sku)
    return session.scalars(statement).all()
