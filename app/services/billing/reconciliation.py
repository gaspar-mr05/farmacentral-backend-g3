"""Read-only remote reconciliation; each successful window commits atomically."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clients.billing import (
    BillingError,
    BillingInvalidResponseError,
    BillingRateLimitError,
)
from app.core.config import Settings
from app.models import (
    BillingSyncState,
    BusinessPeriod,
    Invoice,
    InvoiceSide,
    InvoiceStatus,
)

logger = logging.getLogger(__name__)
WINDOW = timedelta(days=3)
OVERLAP = timedelta(minutes=10)


class BillingReconciliationService:
    def __init__(
        self, client, session: Session, period: BusinessPeriod, *, settings: Settings
    ):
        if period.group_id != settings.farma_central_group:
            raise ValueError("Billing period belongs to another group")
        self.client = client
        self.session = session
        self.period = period
        self.settings = settings
        self.blocked = False
        self.result = {"queries": 0, "observations": 0, "failed": 0}

    def _state(self, stream: str) -> BillingSyncState:
        state = self.session.get(BillingSyncState, (self.period.id, stream))
        if state is None:
            state = BillingSyncState(period_id=self.period.id, stream=stream)
            self.session.add(state)
            self.session.commit()
        return state

    async def poll(self, *, now: datetime | None = None) -> dict:
        self.blocked = False
        self.result = {"queries": 0, "observations": 0, "failed": 0}
        now = now or datetime.now(UTC)
        horizon = min(now, self.period.ends_at or now)
        if horizon <= self.period.starts_at:
            return self.result
        cooldown = self.session.scalar(
            select(func.max(BillingSyncState.next_retry_at)).where(
                BillingSyncState.period_id == self.period.id,
                (BillingSyncState.http_status.in_([401, 403, 429]))
                | (BillingSyncState.last_error == "BillingRateLimitError"),
            )
        )
        self.session.commit()
        if cooldown and cooldown > now:
            logger.warning("Billing reconciliation cooldown: retry_at=%s", cooldown)
            return {"queries": 0, "observations": 0, "failed": 1}
        # Revisit old debt first, including its paid result. Progress survives restart.
        for side in InvoiceSide:
            await self._pending(side, now, horizon)
        for side in InvoiceSide:
            for status in InvoiceStatus:
                state = self._state(f"{side.value}:{status.value}")
                start = max(
                    self.period.starts_at,
                    (state.cursor or self.period.starts_at) - OVERLAP,
                )
                end = min(start + WINDOW, horizon)
                if await self._query(state, side, status, start, end, now):
                    state.cursor = max(state.cursor or end, end)
                    self.session.commit()
                # Initial discovery already traverses the entire period. Afterwards
                # a separate cursor periodically reconciles all historical windows.
                if state.cursor and state.cursor >= horizon:
                    await self._full(state, side, status, now, horizon)
                self._warn_stale(state, now, horizon)
        await self._bank(now)
        return self.result

    async def _pending(self, side, now, horizon):
        state = self._state(f"{side.value}:paid")
        if self.blocked or (state.next_retry_at and state.next_retry_at > now):
            return
        query = select(func.min(Invoice.issued_at)).where(
            Invoice.period_id == self.period.id,
            Invoice.side == side,
            Invoice.status == InvoiceStatus.PENDING,
        )
        first = self.session.scalar(
            query.where(
                Invoice.issued_at >= (state.pending_cursor or self.period.starts_at)
            )
        )
        if first is None:
            first = self.session.scalar(query)
        self.session.commit()
        if first is None:
            return
        start = max(
            first.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
            self.period.starts_at,
        )
        end = min(start + timedelta(days=1), horizon)
        for status in InvoiceStatus:
            target = self._state(f"{side.value}:{status.value}")
            if not await self._query(target, side, status, start, end, now):
                return
        state.pending_cursor = end
        self.session.commit()

    async def _full(self, state, side, status, now, horizon):
        if state.full_target is None:
            if state.next_full_at is None:
                # The initial discovery just completed its first full scan.
                state.next_full_at = now + timedelta(
                    seconds=self.settings.billing_full_sync_interval_seconds
                )
                self.session.commit()
                return
            if state.next_full_at > now:
                return
            state.full_cursor = self.period.starts_at
            state.full_target = horizon
            self.session.commit()
        start = state.full_cursor
        end = min(start + WINDOW, state.full_target)
        if not await self._query(state, side, status, start, end, now):
            return
        state.full_cursor = end
        if end >= state.full_target:
            state.full_cursor = state.full_target = None
            state.next_full_at = now + timedelta(
                seconds=self.settings.billing_full_sync_interval_seconds
            )
        self.session.commit()

    async def _query(self, state, side, status, start, end, now) -> bool:
        if self.blocked or (state.next_retry_at and state.next_retry_at > now):
            return False
        state.last_attempt_at = now
        self.session.commit()
        try:
            invoices = await self.client.get_invoices(
                side=side, status=status, from_date=start, to_date=end
            )
            for invoice in invoices:
                party = (
                    invoice.client_id
                    if side == InvoiceSide.CLIENT
                    else invoice.supplier_id
                )
                if (
                    party != str(self.period.group_id)
                    or invoice.status != status
                    or not start <= invoice.issued_at <= end
                ):
                    raise BillingInvalidResponseError("Invoice outside requested scope")
                self._upsert(invoice, side, now)
            state.last_success_at = now
            state.last_error = state.next_retry_at = state.http_status = None
            self.session.commit()
        except BillingError as exc:
            self.session.rollback()
            self._failure(state, exc, now)
            return False
        self.result["queries"] += 1
        self.result["observations"] += len(invoices)
        return True

    def _upsert(self, invoice, side, now):
        existing = self.session.scalar(
            select(Invoice).where(
                Invoice.period_id == self.period.id,
                Invoice.external_invoice_id == invoice.invoice_id,
            )
        )
        counterparty = (
            invoice.supplier_id if side == InvoiceSide.CLIENT else invoice.client_id
        )
        if existing and (
            existing.side != side
            or existing.counterparty_id != counterparty
            or existing.issued_at != invoice.issued_at
        ):
            raise BillingInvalidResponseError("Invoice identity changed")
        values = dict(
            period_id=self.period.id,
            external_invoice_id=invoice.invoice_id,
            side=side,
            counterparty_id=counterparty,
            channel=invoice.channel,
            status=invoice.status,
            principal=invoice.principal,
            interest=invoice.interest,
            total=invoice.total,
            issued_at=invoice.issued_at,
            external_updated_at=invoice.updated_at,
            observed_at=now,
            paid_at=invoice.updated_at
            if invoice.status == InvoiceStatus.PAID
            else None,
        )
        statement = insert(Invoice).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_invoice_external_id",
                set_={
                    "updated_at": now,
                    **{
                        name: getattr(statement.excluded, name)
                        for name in (
                            "status",
                            "principal",
                            "interest",
                            "total",
                            "external_updated_at",
                            "observed_at",
                            "paid_at",
                        )
                    },
                },
                where=(
                    Invoice.external_updated_at
                    <= statement.excluded.external_updated_at
                )
                & (
                    (Invoice.status == InvoiceStatus.PENDING)
                    | (statement.excluded.status == InvoiceStatus.PAID)
                ),
            )
        )
        self.session.expire_all()

    def _failure(self, state, exc, now):
        state.last_error = type(exc).__name__
        state.http_status = exc.status_code
        delay = max(
            self.settings.billing_poll_interval_seconds, exc.retry_after_seconds or 0
        )
        state.next_retry_at = now + timedelta(seconds=delay)
        self.session.commit()
        self.result["failed"] += 1
        self.blocked = isinstance(exc, BillingRateLimitError) or (
            exc.status_code in {401, 403, 429} or exc.retry_after_seconds is not None
        )
        logger.warning(
            "Billing reconciliation failed: stream=%s error=%s http_status=%s",
            state.stream,
            state.last_error,
            exc.status_code,
        )

    def _warn_stale(self, state, now, horizon):
        oldest = now - timedelta(seconds=self.settings.billing_stale_after_seconds)
        if (
            state.last_success_at is None
            or state.last_success_at < oldest
            or state.cursor is None
            or state.cursor < horizon - OVERLAP
        ):
            logger.warning(
                "Billing reconciliation lag: stream=%s last_success_at=%s cursor=%s",
                state.stream,
                state.last_success_at,
                state.cursor,
            )

    async def _bank(self, now):
        state = self._state("bank")
        if self.blocked or (state.next_retry_at and state.next_retry_at > now):
            return
        if state.last_success_at and now - state.last_success_at < timedelta(
            seconds=self.settings.billing_bank_interval_seconds
        ):
            return
        state.last_attempt_at = now
        self.session.commit()
        try:
            statement = await self.client.get_bank_statement()
            if statement.group != self.period.group_id:
                raise BillingInvalidResponseError("Bank statement outside period scope")
            state.balance = statement.balance
            state.last_success_at = now
            state.last_error = state.next_retry_at = state.http_status = None
            self.session.commit()
            self.result["queries"] += 1
        except BillingError as exc:
            self.session.rollback()
            self._failure(state, exc, now)
