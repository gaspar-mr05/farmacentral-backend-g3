"""Preview sourcing for a kit; --execute permits one durable market acquisition."""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError

from app.clients.farma_central import FarmaCentralClient
from app.clients.market_prices import MarketPriceClient
from app.clients.purchase_orders import PurchaseOrdersClient
from app.clients.trades import TradesClient
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.schemas.procurement import ProcurementDemand
from app.services.supply.procurement import ScarcityProcurementService
from app.services.trades.periods import market_period


async def run(demand, *, period_id=None, execute=False, request_key=None):
    settings = get_settings()
    with SessionLocal() as session:
        period = market_period(session, settings, period_id)
        session.commit()
        async with AsyncExitStack() as stack:
            inventory = await stack.enter_async_context(
                FarmaCentralClient(settings=settings)
            )
            trades = await stack.enter_async_context(TradesClient(settings=settings))
            orders = await stack.enter_async_context(
                PurchaseOrdersClient(settings=settings)
            )
            prices = await stack.enter_async_context(
                MarketPriceClient(settings=settings)
            )
            service = ScarcityProcurementService(
                inventory, trades, orders, prices, session, period
            )
            if execute:
                row = await service.ensure(request_key, demand)
                result = {
                    "id": str(row.id),
                    "status": row.status,
                    "decision": row.decision,
                    "next_retry_at": row.next_retry_at,
                    "last_error": row.last_error,
                }
            else:
                result = (await service.preview(demand)).model_dump(mode="json")
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-id", type=UUID)
    parser.add_argument("--sku", required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--buffer", type=int, default=0)
    parser.add_argument(
        "--deadline", required=True, help="ISO datetime with explicit timezone"
    )
    parser.add_argument("--expected-revenue", type=Decimal, required=True)
    parser.add_argument("--other-costs", type=Decimal, default=Decimal(0))
    parser.add_argument("--minimum-margin", type=Decimal, default=Decimal("0.10"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--request-key", help="Stable identifier reused for this demand on every retry"
    )
    args = parser.parse_args()
    if args.execute and not args.request_key:
        parser.error("--execute requires a stable --request-key")
    try:
        demand = ProcurementDemand(
            sku=args.sku,
            quantity=args.quantity,
            buffer=args.buffer,
            deadline=args.deadline,
            expected_revenue=args.expected_revenue,
            other_costs=args.other_costs,
            minimum_margin=args.minimum_margin,
        )
    except ValidationError as exc:
        parser.error(str(exc))
    asyncio.run(
        run(
            demand,
            period_id=args.period_id,
            execute=args.execute,
            request_key=args.request_key,
        )
    )


if __name__ == "__main__":
    main()
