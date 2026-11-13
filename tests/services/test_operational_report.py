from datetime import UTC, datetime, timedelta

from app.models import (
    BillingOperationStatus,
    BillingSyncState,
    BillingTask,
    BillingTaskKind,
    InboundEvent,
    InboundEventSource,
    MessageDirection,
    MessagingDecision,
    MessagingMessage,
    OrderStatus,
    WorkerHeartbeat,
)
from app.services.operational_report import observe
from tests.support.billing import make_invoice, make_period
from tests.support.orders import make_external_order

NOW = datetime(2026, 11, 18, 18, tzinfo=UTC)


def test_operational_risks_are_scoped_and_do_not_modify_business_state(db_session):
    period = make_period(
        starts_at=NOW - timedelta(hours=10), ends_at=NOW + timedelta(days=1)
    )
    old = make_period(code="previous", starts_at=NOW - timedelta(days=5))
    db_session.add_all([period, old])
    db_session.flush()
    db_session.add_all(
        [
            make_invoice(
                period.id,
                external_invoice_id="soon",
                issued_at=NOW - timedelta(hours=5),
            ),
            make_invoice(
                period.id,
                external_invoice_id="late",
                issued_at=NOW - timedelta(hours=6),
            ),
            make_invoice(
                old.id,
                external_invoice_id="historical",
                issued_at=NOW - timedelta(days=2),
            ),
            BillingTask(
                period_id=period.id,
                kind=BillingTaskKind.EMIT_INVOICE,
                external_order_id="1" * 24,
                status=BillingOperationStatus.UNCERTAIN,
                updated_at=NOW - timedelta(minutes=10),
            ),
            BillingSyncState(period_id=period.id, stream="bank", http_status=429),
            WorkerHeartbeat(
                period_id=period.id,
                worker_name="process_billing",
                heartbeat_at=NOW - timedelta(minutes=10),
                last_started_at=NOW,
                consecutive_failures=1,
            ),
            make_external_order(
                status=OrderStatus.RECEIVED,
                created_at=NOW - timedelta(hours=1),
                delivery_deadline=NOW + timedelta(minutes=5),
            ),
        ]
    )
    db_session.flush()
    report = observe(db_session, period, now=NOW)
    alerts = report["alerts"]
    assert {item["kind"] for item in alerts} >= {
        "payment_due_soon",
        "payment_overdue",
        "billing_tasks_stalled",
        "worker_missing",
        "worker_unhealthy",
        "remote_rate_limit",
        "order_deadline_near",
    }
    assert not any(item.get("invoice_id") == "historical" for item in alerts)
    assert report["waiting_orders"][0]["age_seconds"] == 3600
    assert report["orders_by_channel"] == [
        {
            "source": "sftp",
            "status": "received",
            "official_status": "creada",
            "count": 1,
        }
    ]
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_inquiry_classification_is_not_treated_as_text_delivery(db_session):
    period = make_period(
        starts_at=NOW - timedelta(hours=1), ends_at=NOW + timedelta(days=1)
    )
    event = InboundEvent(
        source=InboundEventSource.MESSAGING,
        external_event_id="msg-test",
        received_at=NOW,
        payload={},
        decision=MessagingDecision.INQUIRY,
        responded_at=NOW,
    )
    db_session.add_all([period, event])
    db_session.flush()
    assert any(
        row["kind"] == "inquiries_without_text"
        for row in observe(db_session, period, now=NOW)["alerts"]
    )
    db_session.add(
        MessagingMessage(
            inbound_event_id=event.id,
            direction=MessageDirection.OUTBOUND,
            phone_normalized="+56900000000",
            payload={},
            occurred_at=NOW,
            group_id=3,
            environment="dev",
            period_id=period.id,
        )
    )
    db_session.flush()
    assert not any(
        row["kind"] == "inquiries_without_text"
        for row in observe(db_session, period, now=NOW)["alerts"]
    )


def test_after_end_financial_observation_excludes_new_invoices(db_session):
    period = make_period(starts_at=NOW - timedelta(hours=10), ends_at=NOW)
    db_session.add(period)
    db_session.flush()
    db_session.add(make_invoice(period.id, issued_at=NOW + timedelta(seconds=1)))
    db_session.flush()
    report = observe(db_session, period, now=NOW + timedelta(minutes=1))
    assert report["metrics"]["invoiced_costs"] == "0"
    assert report["metrics"]["financial_as_of"] == NOW.isoformat().replace(
        "+00:00", "Z"
    )
