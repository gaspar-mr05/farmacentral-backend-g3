"""Pay official client invoices, reconciling by ID before every payment."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.clients.billing import BillingError, BillingInvalidResponseError
from app.models import (
    BillingAttempt,
    BillingTask,
    BillingTaskKind,
    Invoice,
    InvoiceSide,
    InvoiceStatus,
)
from app.models import BillingOperationStatus as Status
from app.schemas.billing import BillingInvoice

from .invoices import upsert_invoice
from .operations import block_billing
from .periods import billing_cooldown
from .queue import enqueue_payment

logger = logging.getLogger(__name__)


def estimated_interest(principal, issued_at, now):
    hours = max(0, (now - issued_at) // timedelta(hours=1) - 6)
    return Decimal(principal) * Decimal("0.01") * hours


class InvoicePaymentService:
    def __init__(self, billing, session, period, *, settings):
        if period.group_id != settings.farma_central_group:
            raise ValueError("Billing period belongs to another group")
        self.billing, self.session, self.period, self.settings = (
            billing,
            session,
            period,
            settings,
        )
        self._clock = lambda: datetime.now(UTC)

    def _pending(self):
        return select(Invoice).where(
            Invoice.period_id == self.period.id,
            Invoice.side == InvoiceSide.CLIENT,
            Invoice.status == InvoiceStatus.PENDING,
        )

    def _sweep(self):
        invoices = self.session.scalars(
            self._pending()
            .where(
                ~exists(
                    select(BillingTask.id).where(
                        BillingTask.period_id == self.period.id,
                        BillingTask.kind == BillingTaskKind.PAY_INVOICE,
                        BillingTask.invoice_id == Invoice.id,
                    )
                )
            )
            .order_by(Invoice.issued_at, Invoice.id)
            .limit(self.settings.billing_payments_batch_size)
        ).all()
        for invoice in invoices:
            enqueue_payment(self.session, invoice)
        self.session.commit()

    async def poll(self, *, now=None):
        self._clock = (lambda: now) if now is not None else lambda: datetime.now(UTC)
        now = self._clock()
        if now < self.period.starts_at:
            return self.coverage(now)
        self._sweep()
        cooldown = billing_cooldown(self.session, self.period.id)
        self.session.commit()
        if cooldown and cooldown > now:
            return self.coverage(now) | {"blocked": 1}
        tasks = self.session.scalars(
            select(BillingTask)
            .join(Invoice, BillingTask.invoice_id == Invoice.id)
            .where(
                BillingTask.period_id == self.period.id,
                BillingTask.kind == BillingTaskKind.PAY_INVOICE,
                BillingTask.status != Status.CONFIRMED,
                or_(
                    BillingTask.next_retry_at.is_(None),
                    BillingTask.next_retry_at <= now,
                ),
            )
            .order_by(Invoice.issued_at, BillingTask.id)
            .limit(self.settings.billing_payments_batch_size)
        ).all()
        blocked = 0
        for task in tasks:
            if await self._process(task):
                blocked = 1
                break
        return self.coverage(self._clock()) | {"blocked": blocked}

    def coverage(self, now):
        confirmed = self.session.scalar(
            select(func.count())
            .select_from(BillingTask)
            .where(
                BillingTask.period_id == self.period.id,
                BillingTask.kind == BillingTaskKind.PAY_INVOICE,
                BillingTask.status == Status.CONFIRMED,
            )
        )
        uncertain = self.session.scalar(
            select(func.count())
            .select_from(BillingTask)
            .where(
                BillingTask.period_id == self.period.id,
                BillingTask.kind == BillingTaskKind.PAY_INVOICE,
                BillingTask.status.in_([Status.IN_FLIGHT, Status.UNCERTAIN]),
            )
        )
        pending = self.session.scalars(self._pending()).all()
        overdue = 0
        for invoice in pending:
            age = now - invoice.issued_at
            if age < timedelta(hours=4):
                continue
            if age >= timedelta(hours=6):
                level, severity = logging.CRITICAL, "overdue"
                overdue += 1
            elif age >= timedelta(hours=5):
                level, severity = logging.ERROR, "urgent"
            else:
                level, severity = logging.WARNING, "warning"
            task = self.session.scalar(
                select(BillingTask).where(
                    BillingTask.period_id == self.period.id,
                    BillingTask.invoice_id == invoice.id,
                    BillingTask.kind == BillingTaskKind.PAY_INVOICE,
                )
            )
            logger.log(
                level,
                "Unpaid invoice: invoice_id=%s severity=%s deadline=%s "
                "estimated_interest=%s reason=%s",
                invoice.external_invoice_id,
                severity,
                invoice.issued_at + timedelta(hours=6),
                estimated_interest(invoice.principal, invoice.issued_at, now),
                task.last_error if task else "not queued",
            )
        self.session.commit()
        return {
            "confirmed": confirmed,
            "pending": len(pending),
            "uncertain": uncertain,
            "overdue": overdue,
        }

    def _validate(self, invoice, row):
        expected_supplier = {"fabric": "1000", "buffer": "2000"}.get(row.channel)
        if (
            invoice.invoice_id != row.external_invoice_id
            or invoice.client_id != str(self.period.group_id)
            or invoice.supplier_id != row.counterparty_id
            or invoice.supplier_id == str(self.period.group_id)
            or (expected_supplier and invoice.supplier_id != expected_supplier)
            or invoice.principal != row.principal
            or invoice.issued_at != row.issued_at
            or invoice.channel != row.channel
        ):
            raise BillingInvalidResponseError(
                "Invoice identity does not match payable debt"
            )

    async def _lookup(self, row):
        invoice_id = row.external_invoice_id
        start = row.issued_at.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.session.commit()
        # Creation windows, not payment dates: old debts remain observable.
        for status in (InvoiceStatus.PAID, InvoiceStatus.PENDING):
            invoices = await self.billing.get_invoices(
                side=InvoiceSide.CLIENT,
                status=status,
                from_date=start,
                to_date=start + timedelta(days=1),
            )
            matches = [
                invoice for invoice in invoices if invoice.invoice_id == invoice_id
            ]
            if len(matches) > 1:
                raise BillingInvalidResponseError(
                    "Duplicate invoice ID in official response"
                )
            if matches:
                invoice = matches[0]
                self._validate(invoice, row)
                if invoice.status != status:
                    raise BillingInvalidResponseError(
                        "Invoice returned under wrong status"
                    )
                return invoice
        return None

    async def _process(self, task):
        now = self._clock()
        row = self.session.get(Invoice, task.invoice_id)
        if (
            row is None
            or row.period_id != self.period.id
            or row.side != InvoiceSide.CLIENT
        ):
            return self._failure(
                task,
                None,
                BillingInvalidResponseError("Not a payable invoice in this period"),
                now,
            )
        attempt = self.session.scalar(
            select(BillingAttempt)
            .where(BillingAttempt.task_id == task.id)
            .order_by(BillingAttempt.number.desc())
            .limit(1)
        )
        if task.status == Status.IN_FLIGHT:
            task.status = Status.UNCERTAIN
            if attempt and attempt.status == Status.IN_FLIGHT:
                attempt.status, attempt.finished_at = (
                    Status.UNCERTAIN,
                    max(now, attempt.started_at),
                )
            self.session.commit()
        if row.status == InvoiceStatus.PAID:
            self._finish(task, attempt, row, now)
            return False
        if attempt and attempt.response:
            try:
                if attempt.response.get("invoice_id") != row.external_invoice_id:
                    raise ValueError("Receipt target mismatch")
                self._confirm(
                    task,
                    attempt,
                    row,
                    BillingInvoice.model_validate(attempt.response["invoice"]),
                    now,
                )
                return False
            except (
                BillingError,
                IntegrityError,
                ValueError,
                ValidationError,
                KeyError,
            ):
                self.session.rollback()
                # Keep the receipt; only an official observation may supersede it.
        self.session.commit()
        try:
            invoice = await self._lookup(row)
            now = self._clock()
            if invoice is None:
                raise BillingInvalidResponseError(
                    "Invoice absent from pending and paid; do not pay"
                )
            if invoice.status == InvoiceStatus.PAID:
                self._confirm(task, attempt, row, invoice, now)
                return False
            row = upsert_invoice(
                self.session, self.period, invoice, InvoiceSide.CLIENT, now
            )
            if row.status == InvoiceStatus.PAID:
                self._finish(task, attempt, row, now)
                return False
            self.session.commit()
        except (BillingError, IntegrityError) as exc:
            self.session.rollback()
            return self._failure(task, None, exc, self._clock())
        # Reaching here requires fresh pending AND paid queries for this exact ID.
        attempt = BillingAttempt(
            task_id=task.id,
            number=1 if attempt is None else attempt.number + 1,
            started_at=now,
        )
        task.status, task.last_error, task.next_retry_at = Status.IN_FLIGHT, None, None
        self.session.add(attempt)
        self.session.commit()
        try:
            paid = await self.billing.pay_invoice(row.external_invoice_id)
            attempt.response = {
                "invoice_id": row.external_invoice_id,
                "invoice": paid.model_dump(mode="json", by_alias=True),
            }
            self.session.commit()
            self._confirm(task, attempt, row, paid, self._clock())
        except (BillingError, IntegrityError) as exc:
            self.session.rollback()
            return self._failure(task, attempt, exc, self._clock())
        return False

    def _failure(self, task, attempt, exc, now):
        http_status = getattr(exc, "status_code", None)
        uncertain = task.status in {Status.IN_FLIGHT, Status.UNCERTAIN} or getattr(
            exc, "outcome_uncertain", False
        )
        if attempt:
            uncertain = uncertain and (
                isinstance(exc, IntegrityError)
                or getattr(exc, "outcome_uncertain", False)
                or attempt.response is not None
                or http_status == 409
            )
            attempt.status = Status.UNCERTAIN if uncertain else Status.FAILED
            attempt.finished_at = max(now, attempt.started_at)
            attempt.error_type, attempt.http_status = type(exc).__name__, http_status
        task.status = Status.UNCERTAIN if uncertain else Status.FAILED
        task.last_error = type(exc).__name__
        number = attempt.number if attempt else 1
        delay = max(
            self.settings.billing_poll_interval_seconds
            * min(2 ** min(number - 1, 3), 5),
            getattr(exc, "retry_after_seconds", None) or 0,
        )
        if isinstance(exc, BillingInvalidResponseError):
            # Bad observations must not starve valid debts in later batches.
            delay = max(delay, 300)
        task.next_retry_at = now + timedelta(seconds=delay)
        self.session.commit()
        logger.warning(
            "Invoice payment failed: task_id=%s status=%s error=%s http_status=%s",
            task.id,
            task.status.value,
            type(exc).__name__,
            http_status,
        )
        return block_billing(
            self.session, self.period.id, "payments", exc, task.next_retry_at, now
        )

    def _confirm(self, task, attempt, row, invoice, now):
        self._validate(invoice, row)
        if invoice.status != InvoiceStatus.PAID:
            raise BillingInvalidResponseError(
                "Payment response is not paid", outcome_uncertain=True
            )
        row = upsert_invoice(
            self.session, self.period, invoice, InvoiceSide.CLIENT, now
        )
        if attempt and attempt.response is None:
            attempt.response = {
                "invoice_id": row.external_invoice_id,
                "invoice": invoice.model_dump(mode="json", by_alias=True),
                "observation": "reconciled_paid",
            }
        self._finish(task, attempt, row, now)

    def _finish(self, task, attempt, row, now):
        if row.status != InvoiceStatus.PAID:
            raise BillingInvalidResponseError("Payment is not confirmed")
        previous_error = task.last_error
        task.status, task.confirmed_at = Status.CONFIRMED, now
        task.last_error = task.next_retry_at = None
        if attempt and attempt.status in {Status.IN_FLIGHT, Status.UNCERTAIN}:
            attempt.status, attempt.finished_at = (
                Status.CONFIRMED,
                max(now, attempt.started_at),
            )
        self.session.commit()
        logger.info(
            "Invoice payment confirmed: invoice_id=%s late=%s "
            "principal=%s interest=%s total=%s previous_error=%s",
            row.external_invoice_id,
            now >= row.issued_at + timedelta(hours=6),
            row.principal,
            row.interest,
            row.total,
            previous_error,
        )
