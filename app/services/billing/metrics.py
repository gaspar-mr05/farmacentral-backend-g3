from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
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

from .payments import estimated_interest
from .sales import fulfilled_sales_query

ZERO = Decimal(0)
INVOICE_STREAMS = {
    f"{side.value}:{status.value}" for side in InvoiceSide for status in InvoiceStatus
}


class BusinessMetricsService:
    def __init__(self, session: Session, period: BusinessPeriod):
        self.session = session
        self.period = period

    def calculate(
        self, *, now: datetime | None = None, financial_as_of: datetime | None = None
    ) -> BusinessMetrics:
        now = now or datetime.now(UTC)
        statement = select(Invoice).where(Invoice.period_id == self.period.id)
        if financial_as_of is not None:
            statement = statement.where(Invoice.issued_at <= financial_as_of)
        invoices = self.session.scalars(statement).all()
        client_invoices = [row for row in invoices if row.side == InvoiceSide.CLIENT]
        supplier_invoices = [
            row for row in invoices if row.side == InvoiceSide.SUPPLIER
        ]

        income = sum((row.principal for row in supplier_invoices), ZERO)
        costs = sum((row.principal for row in client_invoices), ZERO)
        interest = sum((row.interest for row in client_invoices), ZERO)
        paid_interest = sum(
            (
                row.interest
                for row in client_invoices
                if row.status == InvoiceStatus.PAID
                and (
                    financial_as_of is None
                    or row.paid_at
                    and row.paid_at <= financial_as_of
                )
            ),
            ZERO,
        )
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
        completions = [
            state_by_stream[stream].full_completed_at
            for stream in INVOICE_STREAMS
            if stream in state_by_stream
            and state_by_stream[stream].full_completed_at is not None
        ]
        cursors = [
            state_by_stream[stream].cursor
            for stream in INVOICE_STREAMS
            if stream in state_by_stream and state_by_stream[stream].cursor is not None
        ]
        covered_through = min(cursors) if len(cursors) == len(INVOICE_STREAMS) else None
        last_complete = (
            min(*completions, covered_through)
            if len(completions) == len(INVOICE_STREAMS) and covered_through
            else None
        )
        freshness_age = (
            max(0, int((now - last_complete).total_seconds()))
            if last_complete
            else None
        )
        bank = state_by_stream.get("bank")
        purchases, sales = self._market_counts(as_of=financial_as_of)

        return BusinessMetrics(
            period_id=str(self.period.id),
            environment=self.period.environment,
            period_code=self.period.code,
            calculated_at=now,
            financial_as_of=financial_as_of or now,
            invoiced_income=income,
            invoiced_costs=costs,
            client_interest=interest,
            client_interest_paid=paid_interest,
            client_interest_pending=interest - paid_interest,
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
                covered_through=covered_through,
                full_scan_pending=any(
                    stream not in state_by_stream
                    or state_by_stream[stream].full_target is not None
                    or state_by_stream[stream].full_completed_at is None
                    for stream in INVOICE_STREAMS
                ),
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
        if is_closing:
            self._validate_closing(now)
        report = self.calculate(
            now=now, financial_as_of=self.period.ends_at if is_closing else None
        )
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

    def _validate_closing(self, now):
        cutoff = self.period.ends_at
        if cutoff is None or now < cutoff:
            raise ValueError("Closing requires a finished accounting period")
        maximum_age = timedelta(seconds=get_settings().billing_stale_after_seconds)
        states = {
            state.stream: state
            for state in self.session.scalars(
                select(BillingSyncState).where(
                    BillingSyncState.period_id == self.period.id
                )
            )
        }
        for stream in INVOICE_STREAMS:
            state = states.get(stream)
            if (
                state is None
                or state.cursor is None
                or state.cursor < cutoff
                or state.full_started_at is None
                or state.full_started_at < cutoff
                or state.full_completed_at is None
                or state.full_completed_at < state.full_started_at
                or not timedelta(0) <= now - state.full_completed_at <= maximum_age
                or state.full_target is not None
                or state.last_error
            ):
                raise ValueError(
                    "Closing requires a recent full reconciliation after period end; "
                    "run reconcile_billing --read-only --full --once"
                )
        invoices = self.session.scalars(
            select(Invoice).where(
                Invoice.period_id == self.period.id,
                Invoice.side == InvoiceSide.CLIENT,
                Invoice.issued_at <= cutoff,
            )
        )
        for invoice in invoices:
            if invoice.status == InvoiceStatus.PAID:
                if invoice.paid_at is None:
                    raise ValueError("Cannot establish payment time for closing")
                if invoice.paid_at <= cutoff:
                    continue
            if invoice.observed_at < cutoff or now - invoice.observed_at > maximum_age:
                raise ValueError("Closing has stale pending invoice observations")
            if invoice.interest != estimated_interest(
                invoice.principal, invoice.issued_at, cutoff
            ):
                raise ValueError(
                    "Observed interest does not establish the period-end amount; "
                    "preserve closing evidence before interest changes"
                )

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

    def _market_counts(self, *, as_of=None) -> tuple[int, int]:
        statement = select(MarketTrade.role).where(
            MarketTrade.period_id == self.period.id
        )
        if as_of is not None:
            statement = statement.where(MarketTrade.closed_at <= as_of)
        roles = self.session.scalars(statement).all()
        return roles.count(MarketRole.BUYER), roles.count(MarketRole.SELLER)
