from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models import (
    BusinessPeriod,
    Invoice,
    InvoiceSide,
    InvoiceStatus,
    MarketRole,
    MarketTrade,
)


def make_period(**overrides):
    now = datetime.now(UTC)
    return BusinessPeriod(
        **(
            {
                "environment": "dev",
                "code": "e3-2026",
                "group_id": 3,
                "starts_at": now,
                "ends_at": now + timedelta(hours=60),
            }
            | overrides
        )
    )


def make_invoice(period_id, **overrides):
    now = datetime.now(UTC)
    return Invoice(
        **(
            {
                "period_id": period_id,
                "external_invoice_id": "invoice-1",
                "side": InvoiceSide.CLIENT,
                "counterparty_id": "1000",
                "channel": "fabric",
                "status": InvoiceStatus.PENDING,
                "principal": Decimal("100.123456789"),
                "interest": Decimal("0.000000001"),
                "total": Decimal("100.123456790"),
                "issued_at": now,
                "external_updated_at": now,
            }
            | overrides
        )
    )


def make_trade(period_id, **overrides):
    return MarketTrade(
        **(
            {
                "period_id": period_id,
                "external_offer_id": "offer-1",
                "external_order_id": "111111111111111111111111",
                "role": MarketRole.BUYER,
                "counterparty_id": "4",
                "sku": "SOB-PAPEL-EST",
                "quantity": 3,
                "unit_price": Decimal("1.123456789"),
                "closed_at": datetime.now(UTC),
            }
            | overrides
        )
    )
