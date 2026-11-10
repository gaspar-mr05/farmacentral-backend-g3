from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class InvoicingCoverage(BaseModel):
    fulfilled_sales: int
    confirmed: int
    uncertain: int
    missing: int
    ratio: Decimal


class PendingPayments(BaseModel):
    count: int
    oldest_issued_at: datetime | None
    oldest_age_seconds: int


class ReconciliationFreshness(BaseModel):
    last_complete_at: datetime | None
    age_seconds: int | None


class BusinessMetrics(BaseModel):
    period_id: str
    environment: str
    period_code: str
    calculated_at: datetime
    invoiced_income: Decimal
    invoiced_costs: Decimal
    client_interest: Decimal
    margin: Decimal
    costs_by_channel: dict[str, Decimal]
    bank_balance: Decimal | None
    invoicing: InvoicingCoverage
    pending_payments: PendingPayments
    reconciliation: ReconciliationFreshness
    market_purchases: int
    market_sales: int
