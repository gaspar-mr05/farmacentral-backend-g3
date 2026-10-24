"""Read-only audit of units that still lack an official lot identity. No API calls."""

import argparse
import json

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Lot, Product, Unit


def audit(session: Session, sku: str | None = None) -> list[dict]:
    statement = (
        select(
            Product.sku,
            Lot.external_lot_id,
            Unit.status,
            func.count(Unit.id).label("units"),
        )
        .join(Lot, Lot.product_id == Product.id)
        .join(Unit, Unit.lot_id == Lot.id)
        .where(
            Lot.external_lot_id.startswith("unreported:")
            | Lot.external_lot_id.startswith("production:")
        )
        .group_by(Product.sku, Lot.external_lot_id, Unit.status)
        .order_by(Product.sku, Lot.external_lot_id, Unit.status)
    )
    if sku is not None:
        statement = statement.where(Product.sku == sku)
    return [dict(row) for row in session.execute(statement).mappings()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sku", help="Limit the audit to this SKU")
    args = parser.parse_args()
    with SessionLocal() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        rows = audit(session, args.sku)
    print(
        json.dumps(
            {"unresolved_units": sum(row["units"] for row in rows), "lots": rows},
            indent=2,
        )
    )
    if rows:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
