from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BillingOperationStatus,
    BillingSyncState,
    BillingTask,
    BillingTaskKind,
    BusinessMetricSnapshot,
    BusinessPeriod,
    Invoice,
    InvoiceSide,
    InvoiceStatus,
    MarketRole,
    MarketTrade,
)
from app.schemas.business_metrics import (
    BusinessMetrics,
    InvoicingCoverage,
    PendingPayments,
    ReconciliationFreshness,
)

from .sales import fulfilled_sales_query

ZERO = Decimal(0)
INVOICE_STREAMS = {
    f"{side.value}:{status.value}" for side in InvoiceSide for status in InvoiceStatus
}


class BusinessMetricsService:
    def __init__(self, session: Session, period: BusinessPeriod):
        self.session = session
        self.period = period

    def calculate(self, *, now: datetime | None = None) -> BusinessMetrics:
        now = now or datetime.now(UTC)
        invoices = self.session.scalars(
            select(Invoice).where(Invoice.period_id == self.period.id)
        ).all()
        client_invoices = [row for row in invoices if row.side == InvoiceSide.CLIENT]
        supplier_invoices = [
            row for row in invoices if row.side == InvoiceSide.SUPPLIER
        ]

        income = sum((row.principal for row in supplier_invoices), ZERO)
        costs = sum((row.principal for row in client_invoices), ZERO)
        interest = sum((row.interest for row in client_invoices), ZERO)
        cost_channels: dict[str, Decimal] = {
            "fabric": ZERO,
            "buffer": ZERO,
            "market": ZERO,
            "other": ZERO,
        }
        for invoice in client_invoices:
            channel = invoice.channel.strip().lower()
            key = "market" if channel in {"market", "mercado"} else channel
            key = key if key in cost_channels else "other"
            cost_channels[key] += invoice.principal

        pending = [
            row for row in client_invoices if row.status == InvoiceStatus.PENDING
        ]
        oldest = min((row.issued_at for row in pending), default=None)
        oldest_age = max(0, int((now - oldest).total_seconds())) if oldest else 0

        sync_states = self.session.scalars(
            select(BillingSyncState).where(
                BillingSyncState.period_id == self.period.id,
                BillingSyncState.stream.in_(INVOICE_STREAMS | {"bank"}),
            )
        ).all()
        state_by_stream = {state.stream: state for state in sync_states}
        successes = [
            state_by_stream[stream].last_success_at
            for stream in INVOICE_STREAMS
            if stream in state_by_stream
            and state_by_stream[stream].last_success_at is not None
        ]
        last_complete = (
            min(successes) if len(successes) == len(INVOICE_STREAMS) else None
        )
        freshness_age = (
            max(0, int((now - last_complete).total_seconds()))
            if last_complete
            else None
        )
        bank = state_by_stream.get("bank")
        purchases, sales = self._market_counts()

        return BusinessMetrics(
            period_id=str(self.period.id),
            environment=self.period.environment,
            period_code=self.period.code,
            calculated_at=now,
            invoiced_income=income,
            invoiced_costs=costs,
            client_interest=interest,
            margin=income - costs - interest,
            costs_by_channel=cost_channels,
            bank_balance=bank.balance if bank else None,
            invoicing=self._invoicing_coverage(),
            pending_payments=PendingPayments(
                count=len(pending),
                oldest_issued_at=oldest,
                oldest_age_seconds=oldest_age,
            ),
            reconciliation=ReconciliationFreshness(
                last_complete_at=last_complete,
                age_seconds=freshness_age,
            ),
            market_purchases=purchases,
            market_sales=sales,
        )

    def capture(
        self,
        commit_sha: str,
        *,
        now: datetime | None = None,
        is_closing: bool = False,
        minimum_interval: timedelta | None = None,
    ) -> BusinessMetricSnapshot | None:
        now = now or datetime.now(UTC)
        commit_sha = commit_sha.strip()
        if not commit_sha or len(commit_sha) > 64:
            raise ValueError("commit_sha must contain at most 64 characters")
        if minimum_interval and not is_closing:
            latest = self.session.scalar(
                select(BusinessMetricSnapshot.captured_at)
                .where(BusinessMetricSnapshot.period_id == self.period.id)
                .order_by(BusinessMetricSnapshot.captured_at.desc())
                .limit(1)
            )
            if latest and now - latest < minimum_interval:
                return None
        report = self.calculate(now=now)
        snapshot = BusinessMetricSnapshot(
            period_id=self.period.id,
            captured_at=now,
            commit_sha=commit_sha,
            is_closing=is_closing,
            metrics=report.model_dump(mode="json"),
        )
        self.session.add(snapshot)
        self.session.commit()
        return snapshot

    def _invoicing_coverage(self) -> InvoicingCoverage:
        sales = set(
            self.session.scalars(select(fulfilled_sales_query(self.period).c.order_id))
        )
        invoices = set(
            self.session.scalars(
                select(Invoice.external_order_id).where(
                    Invoice.period_id == self.period.id,
                    Invoice.side == InvoiceSide.SUPPLIER,
                    Invoice.external_order_id.in_(sales),
                )
            )
        )
        tasks = self.session.execute(
            select(BillingTask.external_order_id, BillingTask.status).where(
                BillingTask.period_id == self.period.id,
                BillingTask.kind == BillingTaskKind.EMIT_INVOICE,
                BillingTask.external_order_id.in_(sales),
            )
        ).all()
        confirmed = invoices | {
            order_id
            for order_id, status in tasks
            if status == BillingOperationStatus.CONFIRMED
        }
        uncertain = {
            order_id
            for order_id, status in tasks
            if status
            in {BillingOperationStatus.IN_FLIGHT, BillingOperationStatus.UNCERTAIN}
        } - confirmed
        missing = sales - confirmed - uncertain
        ratio = Decimal(len(confirmed)) / len(sales) if sales else ZERO
        return InvoicingCoverage(
            fulfilled_sales=len(sales),
            confirmed=len(confirmed),
            uncertain=len(uncertain),
            missing=len(missing),
            ratio=ratio,
        )

    def _market_counts(self) -> tuple[int, int]:
        roles = self.session.scalars(
            select(MarketTrade.role).where(MarketTrade.period_id == self.period.id)
        ).all()
        return roles.count(MarketRole.BUYER), roles.count(MarketRole.SELLER)
