from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    BillingAttempt,
    BillingOperationStatus,
    BillingTask,
    BillingTaskKind,
    InvoiceSide,
    InvoiceStatus,
    MarketTrade,
)
from tests.services.test_recalls import recall_database as recall_database
from tests.support.billing import make_invoice, make_period, make_trade
from tests.support.orders import make_external_order


@pytest.fixture
def period(db_session):
    value = make_period()
    db_session.add(value)
    db_session.flush()
    return value


def test_period_distinguishes_environment_group_and_reset_epoch(db_session, period):
    variants = [
        make_period(environment="prod"),
        make_period(group_id=4),
        make_period(code="e2-2026"),
    ]
    db_session.add_all(variants)
    db_session.flush()
    assert len({p.id for p in [period, *variants]}) == 4
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(make_period())
        db_session.flush()
    assert error.value.orig.diag.constraint_name == "uq_business_period"


@pytest.mark.parametrize("side", list(InvoiceSide))
def test_invoice_preserves_decimal_snapshot_without_guessing_order(
    db_session, period, side
):
    invoice = make_invoice(period.id, side=side)
    db_session.add(invoice)
    db_session.flush()
    db_session.expire(invoice)
    assert invoice.principal == Decimal("100.123456789")
    assert invoice.interest == Decimal("0.000000001")
    assert invoice.total == Decimal("100.123456790")
    assert invoice.external_order_id is invoice.sale_order_id is None
    assert invoice.status is InvoiceStatus.PENDING
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = datetime.now(UTC)
    db_session.flush()
    db_session.expire(invoice)
    assert invoice.status is InvoiceStatus.PAID
    assert invoice.paid_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("field", ["principal", "interest", "total"])
@pytest.mark.parametrize("value", ["-0.01", "NaN", "Infinity", "-Infinity"])
def test_invoice_rejects_negative_and_nonfinite_money(db_session, period, field, value):
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(make_invoice(period.id, **{field: Decimal(value)}))
        db_session.flush()


def test_invoice_identity_is_unique_per_period_and_cannot_be_issued_twice(
    db_session, period
):
    invoice = make_invoice(period.id)
    db_session.add(invoice)
    db_session.flush()
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(make_invoice(period.id))
        db_session.flush()
    assert error.value.orig.diag.constraint_name == "uq_invoice_external_id"

    other = make_period(code="e3-reset-rehearsal")
    db_session.add(other)
    db_session.flush()
    db_session.add(make_invoice(other.id))
    invoice.external_order_id = "official-order"
    db_session.flush()
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(
            make_invoice(
                period.id,
                external_invoice_id="invoice-2",
                external_order_id="official-order",
            )
        )
        db_session.flush()
    assert error.value.orig.diag.constraint_name == "uq_invoice_external_order"


def test_market_purchase_has_its_own_counterparty_and_does_not_create_sale(
    db_session, period
):
    trade = make_trade(period.id)
    db_session.add(trade)
    db_session.flush()
    db_session.expire(trade)
    assert trade.sale_order is None
    assert trade.counterparty_id == "4"
    assert trade.total == Decimal("3.370370367")
    assert db_session.scalar(select(func.count()).select_from(MarketTrade)) == 1


def test_purchase_and_cost_invoice_cannot_reference_a_local_sale(db_session, period):
    order = make_external_order()
    db_session.add(order)
    db_session.flush()
    for item in (
        make_trade(period.id, sale_order_id=order.id),
        make_invoice(
            period.id, sale_order_id=order.id, external_order_id=order.external_order_id
        ),
    ):
        with pytest.raises(IntegrityError), db_session.begin_nested():
            db_session.add(item)
            db_session.flush()


@pytest.mark.parametrize("use_relationship", [False, True])
def test_invoice_and_task_cannot_link_data_from_another_period(
    db_session, period, use_relationship
):
    other = make_period(environment="prod")
    db_session.add(other)
    db_session.flush()
    trade = make_trade(other.id)
    invoice = make_invoice(other.id)
    db_session.add_all([trade, invoice])
    db_session.flush()
    targets = (
        make_invoice(
            period.id,
            **(
                {"market_trade": trade}
                if use_relationship
                else {"market_trade_id": trade.id}
            ),
        ),
        BillingTask(
            period_id=period.id,
            kind=BillingTaskKind.PAY_INVOICE,
            **(
                {"invoice": invoice} if use_relationship else {"invoice_id": invoice.id}
            ),
        ),
    )
    for item in targets:
        with pytest.raises(IntegrityError), db_session.begin_nested():
            db_session.add(item)
            db_session.flush()


@pytest.mark.parametrize("kind", list(BillingTaskKind))
def test_billing_task_deduplicates_the_stable_target(db_session, period, kind):
    invoice = make_invoice(period.id)
    db_session.add(invoice)
    db_session.flush()
    target = (
        {"external_order_id": "official-order"}
        if kind is BillingTaskKind.EMIT_INVOICE
        else {"invoice_id": invoice.id}
    )
    db_session.add(BillingTask(period_id=period.id, kind=kind, **target))
    db_session.flush()
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(BillingTask(period_id=period.id, kind=kind, **target))
        db_session.flush()


@pytest.mark.parametrize("kind", list(BillingTaskKind))
def test_billing_task_requires_a_target(db_session, period, kind):
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(BillingTask(period_id=period.id, kind=kind))
        db_session.flush()


def test_attempt_preserves_unknown_outcome_after_session_restart(recall_database):
    now = datetime.now(UTC)
    with Session(recall_database) as session:
        period = make_period()
        session.add(period)
        session.flush()
        task = BillingTask(
            period_id=period.id,
            kind=BillingTaskKind.EMIT_INVOICE,
            external_order_id="official-order",
            status=BillingOperationStatus.IN_FLIGHT,
        )
        attempt = BillingAttempt(task=task, number=1, started_at=now)
        session.add(attempt)
        session.commit()
        task_id, attempt_id = task.id, attempt.id
    with Session(recall_database) as session:
        task = session.get(BillingTask, task_id)
        attempt = session.get(BillingAttempt, attempt_id)
        assert task.status is attempt.status is BillingOperationStatus.IN_FLIGHT
        assert attempt.finished_at is None
        task.status = attempt.status = BillingOperationStatus.UNCERTAIN
        task.next_retry_at = now + timedelta(minutes=1)
        attempt.finished_at = now + timedelta(seconds=20)
        attempt.error_type = "SOAPTimeout"
        session.commit()
    with Session(recall_database) as session:
        task = session.get(BillingTask, task_id)
        assert task.status is BillingOperationStatus.UNCERTAIN
        assert task.invoice_id is None
        assert task.attempts[0].error_type == "SOAPTimeout"
        assert task.next_retry_at == now + timedelta(minutes=1)


def test_only_one_attempt_can_be_in_flight_and_numbers_cannot_repeat(
    db_session, period
):
    task = BillingTask(
        period_id=period.id,
        kind=BillingTaskKind.EMIT_INVOICE,
        external_order_id="official-order",
    )
    first = BillingAttempt(task=task, number=1, started_at=datetime.now(UTC))
    db_session.add(first)
    db_session.flush()
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(BillingAttempt(task_id=task.id, number=2))
        db_session.flush()
    assert error.value.orig.diag.constraint_name == "uq_billing_attempt_in_flight"
    first.status = BillingOperationStatus.UNCERTAIN
    first.finished_at = datetime.now(UTC)
    db_session.flush()
    db_session.add(BillingAttempt(task_id=task.id, number=2))
    db_session.flush()
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            BillingAttempt(
                task_id=task.id,
                number=1,
                status=BillingOperationStatus.FAILED,
                started_at=first.started_at,
                finished_at=first.finished_at,
            )
        )
        db_session.flush()


def test_two_workers_cannot_create_two_emission_tasks(recall_database):
    with Session(recall_database) as session:
        period = make_period()
        session.add(period)
        session.commit()
        period_id = period.id
    ready = Barrier(2)

    def insert():
        with Session(recall_database) as session:
            session.add(
                BillingTask(
                    period_id=period_id,
                    kind=BillingTaskKind.EMIT_INVOICE,
                    external_order_id="same-order",
                )
            )
            ready.wait(timeout=5)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return "duplicate"
            return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(insert), executor.submit(insert)
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [
            "created",
            "duplicate",
        ]
    with Session(recall_database) as session:
        assert session.scalar(select(func.count()).select_from(BillingTask)) == 1
