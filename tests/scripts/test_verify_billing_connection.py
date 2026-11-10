from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from app.clients.billing import BillingAuthenticationError
from app.schemas.billing import BankStatement
from scripts import verify_billing_connection as module
from tests.support.farma_central import make_settings


@pytest.mark.anyio
async def test_probe_can_only_read_bank_and_invoice_lists(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_bank_statement.return_value = BankStatement(
        group=3, balance=Decimal("100.123456789")
    )
    client.get_invoices.return_value = []
    monkeypatch.setattr(module, "BillingClient", Mock(return_value=client))
    monkeypatch.setattr(module, "get_settings", make_settings)

    report = await module.run(include_invoices=True)

    assert report["checks"]["bank_statement"]["balance"] == "100.123456789"
    assert client.get_invoices.await_count == 4
    client.emit_invoice.assert_not_awaited()
    client.pay_invoice.assert_not_awaited()
    assert report["business_mutations"] == report["llm_calls"] == 0


@pytest.mark.anyio
async def test_probe_reports_auth_failure_without_password_or_fault_text(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_bank_statement.side_effect = BillingAuthenticationError(
        "private-password", operation="getBankStatement", status_code=401
    )
    monkeypatch.setattr(module, "BillingClient", Mock(return_value=client))
    monkeypatch.setattr(module, "get_settings", make_settings)

    report = await module.run()

    assert report["checks"]["billing_error"]["http_status"] == 401
    assert "private-password" not in str(report)
