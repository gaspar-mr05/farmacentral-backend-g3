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
    covered_through: datetime | None = None
    full_scan_pending: bool = True


class BusinessMetrics(BaseModel):
    period_id: str
    environment: str
    period_code: str
    calculated_at: datetime
    financial_as_of: datetime | None = None
    invoiced_income: Decimal
    invoiced_costs: Decimal
    client_interest: Decimal
    client_interest_paid: Decimal = Decimal(0)
    client_interest_pending: Decimal = Decimal(0)
    margin: Decimal
    costs_by_channel: dict[str, Decimal]
    bank_balance: Decimal | None
    invoicing: InvoicingCoverage
    pending_payments: PendingPayments
    reconciliation: ReconciliationFreshness
    market_purchases: int
    market_sales: int
