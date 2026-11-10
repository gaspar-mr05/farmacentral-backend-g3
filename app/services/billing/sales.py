"""Durable sales invoicing. An unknown remote effect is never blindly retried."""

import logging
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.clients.billing import (
    BillingError,
    BillingInvalidResponseError,
    BillingRateLimitError,
    BillingSOAPFault,
)
from app.clients.purchase_orders import PurchaseOrdersError
from app.models import (
    BillingAttempt,
    BillingSyncState,
    BillingTask,
    BillingTaskKind,
    ExternalOrderStatus,
    Invoice,
    InvoiceSide,
    MarketRole,
    MarketTrade,
    Order,
    OrderStatus,
)
from app.models import (
    BillingOperationStatus as Status,
)
from app.schemas.billing import BillingInvoice
from app.schemas.purchase_orders import PurchaseOrderId, PurchaseOrderState

from .invoices import upsert_invoice
from .periods import billing_cooldown
from .queue import enqueue_sale

logger = logging.getLogger(__name__)


class SalesInvoicingService:
    def __init__(self, billing, orders, session, period, *, settings):
        self.billing = billing
        self.orders = orders
        self.session = session
        self.period = period
        self.settings = settings
        if period.group_id != settings.farma_central_group:
            raise ValueError("Billing period belongs to another group")

    def _sales(self):
        orders = select(Order.external_order_id.label("order_id")).where(
            Order.external_order_id.is_not(None),
            or_(
                Order.status == OrderStatus.DISPATCHED,
                Order.external_status == ExternalOrderStatus.FULFILLED,
            ),
            Order.created_at >= self.period.starts_at,
            ~exists(
                select(MarketTrade.id).where(
                    MarketTrade.period_id == self.period.id,
                    MarketTrade.external_order_id == Order.external_order_id,
                    MarketTrade.role == MarketRole.BUYER,
                )
            ),
        )
        trades = select(MarketTrade.external_order_id.label("order_id")).where(
            MarketTrade.period_id == self.period.id,
            MarketTrade.role == MarketRole.SELLER,
            MarketTrade.closed_at >= self.period.starts_at,
        )
        if self.period.ends_at:
            orders = orders.where(Order.created_at < self.period.ends_at)
            trades = trades.where(MarketTrade.closed_at < self.period.ends_at)
        return orders.union(trades).subquery()

    def _sweep(self):
        sales = self._sales()
        missing = self.session.scalars(
            select(sales.c.order_id)
            .where(
                ~exists(
                    select(BillingTask.id).where(
                        BillingTask.period_id == self.period.id,
                        BillingTask.kind == BillingTaskKind.EMIT_INVOICE,
                        BillingTask.external_order_id == sales.c.order_id,
                    )
                )
            )
            .limit(self.settings.billing_sales_batch_size)
        ).all()
        for order_id in missing:
            # A market reference is not necessarily an OC ID; never send a local UUID.
            try:
                TypeAdapter(PurchaseOrderId).validate_python(order_id)
            except ValidationError:
                logger.warning("Cannot invoice market sale: invalid official order ID")
                continue
            enqueue_sale(self.session, self.period, order_id.lower())
        self.session.commit()

    async def poll(self, *, now=None):
        now = now or datetime.now(UTC)
        if now < self.period.starts_at:
            return self.coverage()
        cooldown = billing_cooldown(self.session, self.period.id)
        self.session.commit()
        if cooldown and cooldown > now:
            return self.coverage() | {"blocked": 1}
        self._sweep()
        due = self.session.scalars(
            select(BillingTask)
            .where(
                BillingTask.period_id == self.period.id,
                BillingTask.kind == BillingTaskKind.EMIT_INVOICE,
                BillingTask.status != Status.CONFIRMED,
                or_(
                    BillingTask.status != Status.FAILED,
                    BillingTask.next_retry_at.is_not(None),
                ),
                or_(
                    BillingTask.next_retry_at.is_(None),
                    BillingTask.next_retry_at <= now,
                ),
            )
            .order_by(BillingTask.updated_at, BillingTask.id)
            .limit(self.settings.billing_sales_batch_size)
        ).all()
        failures = 0
        for task in due:
            if await self._process(task, now):
                failures += 1
                break  # Shared auth/rate failures must not cause a request burst.
        return self.coverage() | {"blocked": failures}

    def coverage(self):
        sales = self._sales()
        targets = (
            select(sales.c.order_id)
            .union(
                select(BillingTask.external_order_id).where(
                    BillingTask.period_id == self.period.id,
                    BillingTask.kind == BillingTaskKind.EMIT_INVOICE,
                )
            )
            .subquery()
        )
        rows = self.session.execute(
            select(BillingTask.status, func.count())
            .select_from(targets)
            .outerjoin(
                BillingTask,
                (BillingTask.external_order_id == targets.c.order_id)
                & (BillingTask.period_id == self.period.id)
                & (BillingTask.kind == BillingTaskKind.EMIT_INVOICE),
            )
            .group_by(BillingTask.status)
        ).all()
        result = {"confirmed": 0, "missing": 0, "uncertain": 0}
        for status, count in rows:
            key = (
                "confirmed"
                if status == Status.CONFIRMED
                else "uncertain"
                if status in {Status.IN_FLIGHT, Status.UNCERTAIN}
                else "missing"
            )
            result[key] += count
        self.session.commit()
        return result

    async def _process(self, task, now):
        try:
            TypeAdapter(PurchaseOrderId).validate_python(task.external_order_id)
        except ValidationError:
            self._preflight_failure(
                task, "Invalid official order ID", now, permanent=True
            )
            return False
        attempt = self.session.scalar(
            select(BillingAttempt)
            .where(BillingAttempt.task_id == task.id)
            .order_by(BillingAttempt.number.desc())
            .limit(1)
        )
        if task.status == Status.IN_FLIGHT:
            task.status = Status.UNCERTAIN
            task.last_error = "Process interrupted; remote effect unknown"
            if attempt and attempt.status == Status.IN_FLIGHT:
                attempt.status = Status.UNCERTAIN
                attempt.finished_at = max(now, attempt.started_at)
            self.session.commit()
        linked = self.session.scalar(
            select(Invoice).where(
                Invoice.period_id == self.period.id,
                Invoice.external_order_id == task.external_order_id,
                Invoice.side == InvoiceSide.SUPPLIER,
            )
        )
        if linked:
            self._finish(task, attempt, linked, now)
            return False
        self.session.commit()
        try:
            official = await self.orders.get_order(task.external_order_id)
        except PurchaseOrdersError as exc:
            self._preflight_failure(
                task,
                type(exc).__name__,
                now,
                delay=max(60, getattr(exc, "retry_after_seconds", None) or 0),
            )
            return self._block(exc, task.next_retry_at, now)
        if (
            official.id.lower() != task.external_order_id.lower()
            or official.supplier_id != str(self.period.group_id)
            or not (
                self.period.starts_at <= official.created_at
                and (
                    self.period.ends_at is None
                    or official.created_at < self.period.ends_at
                )
            )
        ):
            self._preflight_failure(
                task,
                "Official order outside supplier/period scope",
                now,
                permanent=True,
            )
            return False
        if official.state != PurchaseOrderState.FULFILLED:
            self._preflight_failure(task, "Official order is not fulfilled", now)
            return False
        if attempt and attempt.response:
            try:
                if attempt.response.get("order_id") != task.external_order_id:
                    raise ValueError("Receipt target mismatch")
                invoice = BillingInvoice.model_validate(attempt.response["invoice"])
                self._confirm(task, attempt, invoice, official, now)
            except (
                BillingError,
                IntegrityError,
                ValidationError,
                ValueError,
                KeyError,
            ):
                self.session.rollback()
                self._uncertain(
                    task, "Stored emission receipt could not be verified", now
                )
            return False
        if official.invoiced or task.status == Status.UNCERTAIN:
            self._uncertain(
                task, "Invoice association unavailable; do not re-emit", now
            )
            return False

        attempt = BillingAttempt(
            task_id=task.id,
            number=1 if attempt is None else attempt.number + 1,
            started_at=now,
        )
        task.status = Status.IN_FLIGHT
        task.next_retry_at = task.last_error = None
        self.session.add(attempt)
        self.session.commit()  # Intent must be durable before sending SOAP.
        try:
            invoice = await self.billing.emit_invoice(task.external_order_id)
            attempt.response = {
                "order_id": task.external_order_id,
                "invoice": invoice.model_dump(mode="json", by_alias=True),
            }
            # Preserve the exact association before applying it to the ledger.
            self.session.commit()
            self._confirm(task, attempt, invoice, official, now)
        except (BillingError, IntegrityError) as exc:
            self.session.rollback()
            http_status = getattr(exc, "status_code", None)
            uncertain = (
                isinstance(exc, IntegrityError)
                or getattr(exc, "outcome_uncertain", False)
                or attempt.response is not None
                or http_status == 409
            )
            attempt.status = Status.UNCERTAIN if uncertain else Status.FAILED
            attempt.finished_at = max(now, attempt.started_at)
            attempt.error_type, attempt.http_status = (
                type(exc).__name__,
                http_status,
            )
            task.status = attempt.status
            task.last_error = type(exc).__name__
            permanent = not uncertain and (
                isinstance(exc, BillingSOAPFault) or http_status in {400, 404, 422}
            )
            delay = max(
                60 * min(2 ** min(attempt.number - 1, 6), 60),
                getattr(exc, "retry_after_seconds", None) or 0,
            )
            task.next_retry_at = (
                None
                if permanent
                else now + timedelta(seconds=max(delay, 600 if uncertain else 0))
            )
            self.session.commit()
            logger.warning(
                "Sales invoice failed: order_id=%s status=%s error=%s",
                task.external_order_id,
                task.status.value,
                type(exc).__name__,
            )
            return self._block(exc, task.next_retry_at, now)
        return False

    def _block(self, exc, retry_at, now):
        if getattr(exc, "status_code", None) not in {401, 403, 429} and not isinstance(
            exc, BillingRateLimitError
        ):
            return False
        state = self.session.get(BillingSyncState, (self.period.id, "sales"))
        if state is None:
            state = BillingSyncState(period_id=self.period.id, stream="sales")
            self.session.add(state)
        state.last_attempt_at = now
        state.last_error = type(exc).__name__
        state.http_status = getattr(exc, "status_code", None)
        state.next_retry_at = retry_at or now + timedelta(minutes=1)
        self.session.commit()
        return True

    def _preflight_failure(self, task, reason, now, *, delay=60, permanent=False):
        # Failed validation must not clear an earlier unknown remote effect.
        if task.status != Status.UNCERTAIN:
            task.status = Status.FAILED if permanent else Status.READY
        task.last_error = reason
        task.next_retry_at = (
            now + timedelta(minutes=10)
            if task.status == Status.UNCERTAIN
            else None
            if permanent
            else now + timedelta(seconds=delay)
        )
        self.session.commit()

    def _uncertain(self, task, reason, now):
        task.status = Status.UNCERTAIN
        task.last_error = reason
        task.next_retry_at = now + timedelta(minutes=10)
        self.session.commit()
        logger.warning(
            "Sales invoice uncertain: order_id=%s reason=%s",
            task.external_order_id,
            reason,
        )

    def _confirm(self, task, attempt, invoice, official, now):
        if (
            invoice.supplier_id != str(self.period.group_id)
            or invoice.client_id != official.customer_id
            or invoice.principal != official.quantity * official.unit_price
            or invoice.issued_at < self.period.starts_at
        ):
            raise BillingInvalidResponseError(
                "Emitted invoice does not match official sale", outcome_uncertain=True
            )
        row = upsert_invoice(
            self.session, self.period, invoice, InvoiceSide.SUPPLIER, now
        )
        if row.external_order_id not in {None, task.external_order_id}:
            raise BillingInvalidResponseError(
                "Invoice already linked to another sale", outcome_uncertain=True
            )
        row.external_order_id = task.external_order_id
        trade = self.session.scalar(
            select(MarketTrade).where(
                MarketTrade.period_id == self.period.id,
                MarketTrade.external_order_id == task.external_order_id,
                MarketTrade.role == MarketRole.SELLER,
            )
        )
        order = self.session.scalar(
            select(Order).where(Order.external_order_id == task.external_order_id)
        )
        if trade:
            row.market_trade_id = trade.id
        if (
            order
            and self.period.starts_at <= order.created_at
            and (self.period.ends_at is None or order.created_at < self.period.ends_at)
        ):
            row.sale_order_id = order.id
        self._finish(task, attempt, row, now)

    def _finish(self, task, attempt, invoice, now):
        task.status = Status.CONFIRMED
        task.invoice_id = invoice.id
        task.confirmed_at = now
        task.last_error = task.next_retry_at = None
        if attempt:
            attempt.status = Status.CONFIRMED
            attempt.finished_at = max(now, attempt.started_at)
            attempt.error_type = attempt.http_status = None
        self.session.commit()
