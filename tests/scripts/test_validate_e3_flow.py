from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from app.models import CustodyEvent, CustodyEventType, InvoiceSide, InvoiceStatus
from scripts import validate_e3_flow as module
from tests.services.test_invoice_payments import NOW, setup_payments
from tests.services.test_recalls import recall_database as recall_database
from tests.services.test_trade_custody import add_market_stock
from tests.support.billing import make_trade
from tests.support.farma_central import make_settings
from tests.support.orders import make_purchase_order


@pytest.mark.anyio
async def test_market_observation_preserves_unit_and_batch_ids(db_session):
    period, _, _, units = add_market_stock(db_session, quantity=1)
    trade = make_trade(period.id, sku=units[0].lot.product.sku, quantity=1)
    db_session.add(trade)
    db_session.flush()
    db_session.add(
        CustodyEvent(
            unit_id=units[0].id,
            market_trade_id=trade.id,
            event_type=CustodyEventType.RECEIVED,
        )
    )
    db_session.flush()
    orders, billing, trades = AsyncMock(), AsyncMock(), AsyncMock()
    trades.get_offer.return_value = SimpleNamespace(
        status="closed", order_id=trade.external_order_id
    )
    orders.get_order.return_value = make_purchase_order(
        id=trade.external_order_id,
        sku=trade.sku,
        cliente="3",
        proveedor=trade.counterparty_id,
        cantidad=1,
        despachado=1,
        precioUnitario=trade.unit_price,
        estado="cumplida",
    )
    args = SimpleNamespace(
        order_id=[], trade_id=[trade.id], message_id=[], recall_id=[]
    )
    checks = await module.inspect(
        db_session, period, args, make_settings(), orders, billing, trades
    )
    actual = checks[f"trade:{trade.id}"]
    assert actual["status"] == "passed"
    assert actual["unit_evidence"][0]["unit_id"] == units[0].external_unit_id
    assert actual["unit_evidence"][0]["batch"] == units[0].lot.external_lot_id
    billing.pay_invoice.assert_not_awaited()


@pytest.mark.anyio
async def test_discovery_uses_a_read_only_transaction_and_never_certifies_step24(
    recall_database, monkeypatch
):
    settings = make_settings(
        farma_central_base_url="https://dev.proyecto.2026-2.tallerdeintegracion.cl/farma-central/"
    )
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        module, "create_engine", lambda *args, **kwargs: recall_database
    )
    commands = []

    def capture(connection, cursor, statement, parameters, context, many):
        commands.append(statement)

    event.listen(recall_database, "before_cursor_execute", capture)
    try:
        report = await module.run(
            SimpleNamespace(environment="dev", period_id=None, list_ids=True)
        )
    finally:
        event.remove(recall_database, "before_cursor_execute", capture)
    assert commands[0] == "SET TRANSACTION READ ONLY"
    assert all(command.startswith(("SELECT", "SET")) for command in commands)
    assert report["step24_status"] == report["automated_status"] == "pending"
    assert report["checks"]["discovery"]["periods"] == []


@pytest.mark.parametrize(
    "field",
    [
        "farma_central_base_url",
        "billing_url",
        "trades_base_url",
        "messaging_base_url",
        "purchase_orders_base_url",
    ],
)
def test_rejects_any_production_endpoint_before_validation(field):
    settings = make_settings(
        farma_central_base_url="https://dev.proyecto.2026-2.tallerdeintegracion.cl/farma-central/"
    )
    settings = settings.model_copy(
        update={field: "https://prod.proyecto.2026-2.tallerdeintegracion.cl/"}
    )
    with pytest.raises(ValueError):
        module.require_development(settings, "dev")


@pytest.mark.anyio
async def test_invoice_read_reconciles_paid_without_paying_or_writing(db_session):
    source, row, dto = setup_payments(db_session)
    row.external_order_id = "a" * 24
    db_session.flush()
    official = dto.model_copy(update={"status": InvoiceStatus.PAID})
    billing = AsyncMock()
    billing.get_invoices.side_effect = lambda **kw: (
        [official] if kw["status"] == InvoiceStatus.PAID else []
    )
    result = await module.check_invoice(
        db_session,
        billing,
        source.period,
        row.external_order_id,
        InvoiceSide.CLIENT,
        {},
    )
    assert result["status"] == "passed"
    assert result["official_status"] == "paid"
    billing.pay_invoice.assert_not_awaited()
    billing.emit_invoice.assert_not_awaited()
    assert row.status == InvoiceStatus.PENDING


@pytest.mark.anyio
async def test_missing_invoice_association_stays_pending(db_session):
    source, _, _ = setup_payments(db_session)
    billing = AsyncMock()
    result = await module.check_invoice(
        db_session, billing, source.period, "b" * 24, InvoiceSide.SUPPLIER, {}
    )
    assert result["status"] == "pending"
    billing.get_invoices.assert_not_awaited()


@pytest.mark.parametrize("problem", [None, "routing", "late", "uncertain"])
def test_message_check_separates_local_ack_from_chat_delivery(problem):
    received = NOW
    confirmation = (received + timedelta(seconds=5)).isoformat()
    event = SimpleNamespace(
        responded_at=received + timedelta(seconds=5),
        external_event_id="message",
        consulted_external_order_id="a" * 24,
        payload={
            "receivedAt": received.isoformat(),
            "sender": {"phone": "+56900000001"},
        },
        resolution_evidence={
            "delivery": {
                "classification": {"state": "confirmed", "confirmedAt": confirmation},
                "reply": {
                    "state": "confirmed",
                    "confirmedAt": confirmation,
                    "to": "+56900000001",
                    "replyTo": "message",
                },
            }
        },
    )
    if problem == "routing":
        event.resolution_evidence["delivery"]["reply"]["to"] = "+56900000002"
    if problem == "late":
        event.resolution_evidence["delivery"]["reply"]["confirmedAt"] = (
            received + timedelta(minutes=31)
        ).isoformat()
    if problem == "uncertain":
        event.resolution_evidence["delivery"]["reply"]["state"] = "uncertain"
    result = module.check_message(event)
    assert result["status"] == (
        "passed"
        if problem is None
        else "pending"
        if problem == "uncertain"
        else "failed"
    )


def test_evidence_output_is_never_overwritten(tmp_path):
    target = tmp_path / "private.env"
    target.write_text("unchanged")
    with pytest.raises(SystemExit):
        module.main(["--environment", "dev", "--list-ids", "--output", str(target)])
    assert target.read_text() == "unchanged"
