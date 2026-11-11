from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.billing import BillingHTTPError, BillingSOAPFault
from app.models import BillingSyncState, Invoice, InvoiceSide, InvoiceStatus
from app.schemas.billing import BankStatement, BillingInvoice
from app.services.billing import BillingReconciliationService
from tests.support.billing import make_invoice, make_period
from tests.support.billing_soap import invoice_values
from tests.support.farma_central import make_settings

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def remote_invoice(**overrides):
    return BillingInvoice.model_validate(
        invoice_values(
            **(
                {
                    "createdAt": (NOW - timedelta(hours=1)).isoformat(),
                    "updatedAt": NOW.isoformat(),
                }
                | overrides
            ),
        )
    )


class BillingSource:
    def __init__(self, invoices=()):
        self.invoices = list(invoices)
        self.calls = []
        self.bank_calls = 0
        self.error = None

    async def get_invoices(self, **query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return [
            invoice
            for invoice in self.invoices
            if invoice.status == query["status"]
            and (
                invoice.client_id
                if query["side"] == InvoiceSide.CLIENT
                else invoice.supplier_id
            )
            == "3"
            and query["from_date"] <= invoice.issued_at <= query["to_date"]
        ]

    async def get_bank_statement(self):
        self.bank_calls += 1
        return BankStatement(group=3, balance=Decimal("-100.123456789"))


def setup_service(session, source, *, days=1, **settings):
    period = make_period(
        starts_at=NOW - timedelta(days=days), ends_at=NOW + timedelta(days=2)
    )
    session.add(period)
    session.commit()
    return period, BillingReconciliationService(
        source, session, period, settings=make_settings(**settings)
    )


@pytest.mark.anyio
async def test_discovers_external_costs_and_sales_without_events_or_double_counting(
    db_session,
):
    source = BillingSource(
        [
            remote_invoice(id="fabric", channel="fabric"),
            remote_invoice(id="hourly-buffer", channel="buffer"),
            remote_invoice(id="sale", client="4", supplier="3", status="paid"),
        ]
    )
    period, service = setup_service(db_session, source)
    await service.poll(now=NOW)
    await service.poll(now=NOW + timedelta(minutes=1))

    invoices = db_session.scalars(select(Invoice)).all()
    assert len(invoices) == 3
    assert sum(i.principal for i in invoices) == Decimal("300.370370367")
    assert {i.channel for i in invoices} == {"fabric", "buffer"}
    assert all(i.external_order_id is None for i in invoices)
    assert {(q["side"], q["status"]) for q in source.calls} == {
        (side, status) for side in InvoiceSide for status in InvoiceStatus
    }
    bank = db_session.get(BillingSyncState, (period.id, "bank"))
    assert bank.balance == Decimal("-100.123456789")
    assert source.bank_calls == 1


@pytest.mark.anyio
async def test_old_pending_becomes_paid_after_restart_even_past_discovery_cursor(
    db_session,
):
    created = NOW - timedelta(days=8)
    source = BillingSource(
        [remote_invoice(id="old", status="paid", createdAt=created.isoformat())]
    )
    period, _ = setup_service(db_session, source, days=10)
    db_session.add(
        make_invoice(
            period.id,
            external_invoice_id="old",
            issued_at=created,
            external_updated_at=created,
        )
    )
    for side in InvoiceSide:
        for status in InvoiceStatus:
            db_session.add(
                BillingSyncState(
                    period_id=period.id,
                    stream=f"{side.value}:{status.value}",
                    cursor=NOW - timedelta(minutes=1),
                    next_full_at=NOW + timedelta(hours=1),
                )
            )
    db_session.commit()
    period_id = period.id
    db_session.commit()
    with Session(
        bind=db_session.get_bind(), join_transaction_mode="create_savepoint"
    ) as fresh:
        reloaded = fresh.get(type(period), period_id)
        restarted = BillingReconciliationService(
            source, fresh, reloaded, settings=make_settings()
        )
        await restarted.poll(now=NOW)
    db_session.expire_all()

    invoice = db_session.scalar(select(Invoice))
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_at == NOW
    assert source.calls[0]["to_date"] < NOW - timedelta(days=6)
    assert source.calls[1]["status"] == InvoiceStatus.PAID


@pytest.mark.anyio
async def test_backfills_all_days_in_valid_overlapping_windows_across_restarts(
    db_session,
):
    source = BillingSource(
        [
            remote_invoice(
                id=f"day-{day}",
                status="paid",
                createdAt=(NOW - timedelta(days=day)).isoformat(),
            )
            for day in range(1, 11)
        ]
    )
    period, _ = setup_service(db_session, source, days=11)
    for cycle in range(5):
        # A fresh service reloads committed cursors, as a restarted process does.
        db_session.expire_all()
        await BillingReconciliationService(
            source, db_session, period, settings=make_settings()
        ).poll(now=NOW + timedelta(minutes=cycle))
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 10
    windows = [
        q
        for q in source.calls
        if q["side"] == InvoiceSide.CLIENT and q["status"] == InvoiceStatus.PAID
    ]
    assert windows[0]["from_date"] == period.starts_at
    assert all(
        timedelta(0) <= q["to_date"] - q["from_date"] <= timedelta(days=3)
        for q in source.calls
    )
    assert windows[1]["from_date"] < windows[0]["to_date"]
    assert windows[-1]["to_date"] == NOW + timedelta(minutes=4)


@pytest.mark.anyio
async def test_full_scan_finds_late_historical_invoice_and_keeps_period_boundary(
    db_session,
):
    source = BillingSource()
    period, service = setup_service(db_session, source)
    await service.poll(now=NOW)
    source.invoices = [
        remote_invoice(
            id="late", status="paid", createdAt=(NOW - timedelta(hours=20)).isoformat()
        )
    ]
    await service.poll(now=NOW + timedelta(minutes=11))
    assert db_session.scalar(select(Invoice.external_invoice_id)) == "late"
    assert all(q["from_date"] >= period.starts_at for q in source.calls)


@pytest.mark.anyio
async def test_failed_query_preserves_cursor_data_and_success_timestamp_then_recovers(
    db_session, caplog
):
    source = BillingSource([remote_invoice()])
    period, service = setup_service(db_session, source)
    await service.poll(now=NOW)
    state = db_session.get(BillingSyncState, (period.id, "client:pending"))
    previous = (state.cursor, state.last_success_at)
    source.error = BillingSOAPFault("Server")
    await service.poll(now=NOW + timedelta(minutes=6))
    db_session.refresh(state)
    assert (state.cursor, state.last_success_at) == previous
    assert state.last_error == "BillingSOAPFault"
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 1
    assert "reconciliation lag" in caplog.text
    source.error = None
    await service.poll(now=NOW + timedelta(minutes=8))
    db_session.refresh(state)
    assert state.cursor == NOW + timedelta(minutes=8)
    assert state.last_error is None


@pytest.mark.anyio
async def test_429_stops_cycle_and_honors_retry_after_across_restart(db_session):
    source = BillingSource()
    source.error = BillingHTTPError(
        "private-secret", status_code=429, retry_after_seconds=900
    )
    period, service = setup_service(db_session, source)
    result = await service.poll(now=NOW)
    assert result["failed"] == 1
    assert len(source.calls) == 1
    state = db_session.get(BillingSyncState, (period.id, "client:pending"))
    assert state.next_retry_at == NOW + timedelta(minutes=15)
    assert state.cursor is None
    assert state.last_success_at is None
    assert "private-secret" not in state.last_error
    restarted = BillingReconciliationService(
        source, db_session, period, settings=make_settings()
    )
    assert (await restarted.poll(now=NOW + timedelta(minutes=1)))["failed"] == 1
    assert len(source.calls) == 1
    source.error = None
    assert (await restarted.poll(now=NOW + timedelta(minutes=16)))["failed"] == 0


@pytest.mark.anyio
async def test_empty_success_advances_cursor_and_never_removes_invoices(db_session):
    source = BillingSource()
    period, service = setup_service(db_session, source)
    db_session.add(
        make_invoice(
            period.id,
            issued_at=NOW - timedelta(hours=2),
            external_updated_at=NOW - timedelta(hours=2),
        )
    )
    db_session.commit()
    await service.poll(now=NOW)
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 1
    state = db_session.get(BillingSyncState, (period.id, "supplier:paid"))
    assert state.cursor == state.last_success_at == NOW
    assert state.last_error is None


@pytest.mark.anyio
async def test_stale_pending_response_does_not_regress_paid_invoice(db_session):
    source = BillingSource([remote_invoice(status="paid")])
    period, service = setup_service(db_session, source)
    await service.poll(now=NOW)
    source.invoices = [
        remote_invoice(
            status="pending", updatedAt=(NOW + timedelta(minutes=1)).isoformat()
        )
    ]
    await service.poll(now=NOW + timedelta(minutes=1))
    invoice = db_session.scalar(select(Invoice))
    assert invoice.status == InvoiceStatus.PAID
    assert invoice.paid_at == NOW


@pytest.mark.anyio
async def test_bad_window_rolls_back_entire_response_and_retains_cursor(db_session):
    source = BillingSource()

    async def invalid(**query):
        if (
            query["side"] == InvoiceSide.CLIENT
            and query["status"] == InvoiceStatus.PENDING
        ):
            return [
                remote_invoice(id="valid"),
                remote_invoice(
                    id="outside", createdAt=(NOW - timedelta(days=20)).isoformat()
                ),
            ]
        return []

    source.get_invoices = invalid
    period, service = setup_service(db_session, source)
    result = await service.poll(now=NOW)
    assert result["failed"] == 1
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 0
    assert (
        db_session.get(BillingSyncState, (period.id, "client:pending")).cursor is None
    )


@pytest.mark.anyio
async def test_period_not_started_makes_no_remote_calls(db_session):
    source = BillingSource()
    _, service = setup_service(db_session, source, days=-1)
    assert await service.poll(now=NOW) == {"queries": 0, "observations": 0, "failed": 0}
    assert source.calls == []
    assert source.bank_calls == 0


@pytest.mark.anyio
async def test_full_history_cursor_resumes_after_restart(db_session):
    source = BillingSource()
    period, _ = setup_service(db_session, source, days=10)
    # Prior discovery was complete, but a full sweep stopped three days in.
    for side in InvoiceSide:
        for status in InvoiceStatus:
            db_session.add(
                BillingSyncState(
                    period_id=period.id,
                    stream=f"{side.value}:{status.value}",
                    cursor=NOW,
                    full_cursor=period.starts_at + timedelta(days=3),
                    full_target=NOW - timedelta(minutes=10),
                )
            )
    db_session.commit()
    source.invoices = [
        remote_invoice(
            id="historical",
            status="paid",
            createdAt=(NOW - timedelta(days=6)).isoformat(),
        )
    ]
    await BillingReconciliationService(
        source, db_session, period, settings=make_settings()
    ).poll(now=NOW)
    invoice = db_session.scalar(select(Invoice))
    assert invoice.external_invoice_id == "historical"
    state = db_session.get(BillingSyncState, (period.id, "client:paid"))
    assert state.full_cursor == period.starts_at + timedelta(days=6)
    assert state.full_target == NOW - timedelta(minutes=10)


@pytest.mark.anyio
async def test_invoice_refresh_preserves_explicit_order_link_and_other_period(
    db_session,
):
    source = BillingSource([remote_invoice()])
    period, service = setup_service(db_session, source)
    remote = source.invoices[0]
    original = make_invoice(
        period.id,
        external_invoice_id=remote.invoice_id,
        issued_at=remote.issued_at,
        external_updated_at=remote.updated_at,
        external_order_id="111111111111111111111111",
    )
    other = make_period(code="other", starts_at=period.starts_at)
    db_session.add_all([original, other])
    db_session.flush()
    db_session.add(make_invoice(other.id, external_invoice_id=remote.invoice_id))
    db_session.commit()
    await service.poll(now=NOW)
    db_session.refresh(original)
    assert original.external_order_id == "111111111111111111111111"
    assert db_session.scalar(select(func.count()).select_from(Invoice)) == 2


@pytest.mark.anyio
async def test_bank_failure_preserves_last_known_balance(db_session):
    source = BillingSource()
    period, service = setup_service(db_session, source)
    await service.poll(now=NOW)

    async def bank_error():
        raise BillingSOAPFault("Server")

    source.get_bank_statement = bank_error
    result = await service.poll(now=NOW + timedelta(minutes=11))
    state = db_session.get(BillingSyncState, (period.id, "bank"))
    assert result["failed"] == 1
    assert state.balance == Decimal("-100.123456789")
    assert state.last_success_at == NOW
    assert state.last_error == "BillingSOAPFault"


@pytest.mark.anyio
async def test_linked_sale_invoiced_after_period_close_still_observes_payment(
    db_session,
):
    created = NOW - timedelta(days=1)
    source = BillingSource(
        [
            remote_invoice(
                id="late-emission", status="paid", createdAt=created.isoformat()
            )
        ]
    )
    period, service = setup_service(db_session, source, days=5)
    period.ends_at = NOW - timedelta(days=2)
    db_session.add(
        make_invoice(
            period.id,
            external_invoice_id="late-emission",
            external_order_id="1" * 24,
            issued_at=created,
            external_updated_at=created,
        )
    )
    db_session.commit()
    assert (await service.poll(now=NOW))["failed"] == 0
    assert db_session.scalar(select(Invoice.status)) == InvoiceStatus.PAID


@pytest.mark.anyio
async def test_full_scan_timestamps_require_success_and_can_be_refreshed(db_session):
    source = BillingSource()
    period, service = setup_service(db_session, source)
    await service.poll(now=NOW)
    state = db_session.get(BillingSyncState, (period.id, "client:pending"))
    assert state.full_started_at == state.full_completed_at == NOW
    await service.poll(now=NOW + timedelta(minutes=1), full=True)
    db_session.refresh(state)
    assert (
        state.full_started_at == state.full_completed_at == NOW + timedelta(minutes=1)
    )
    source.error = BillingSOAPFault("Server")
    await service.poll(now=NOW + timedelta(minutes=2), full=True)
    db_session.refresh(state)
    assert state.full_completed_at == NOW + timedelta(minutes=1)
