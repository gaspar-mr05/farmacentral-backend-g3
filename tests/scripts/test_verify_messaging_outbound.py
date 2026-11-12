import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.clients.messaging import MessagingHTTPError, MessagingTimeoutError
from scripts import verify_messaging_outbound as module
from tests.support.farma_central import make_settings


@pytest.fixture
def probe(tmp_path, monkeypatch):
    payload = {
        "messageId": "chat-authentic-id",
        "sender": {"name": "Chat de prueba", "phone": "+56 9 5555 0123"},
        "text": "PRUEBA PASO17 G3",
        "receivedAt": datetime.now(UTC).isoformat(),
    }
    path = tmp_path / "webhook.json"
    path.write_text(json.dumps(payload))
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.send_reply.return_value = {"messageId": "remote-id", "private": "secret"}
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "MessagingClient", factory)
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: make_settings(
            messaging_base_url=f"https://{module.DEV_HOST}/messaging/"
        ),
    )
    return path, payload, client, factory


@pytest.mark.anyio
async def test_probe_sends_one_reply_using_the_original_webhook(probe):
    path, payload, client, _ = probe
    result = await module.run(path, text="Prueba de respuesta.")
    assert result["status"] == "accepted"
    assert result["reply_to"] == payload["messageId"]
    assert result["llm_calls"] == 0
    assert result["chat_delivery_verified"] is False
    assert result["confirmation_fields"] == ["messageId", "private"]
    assert "secret" not in str(result)
    client.send_reply.assert_awaited_once()
    original = client.send_reply.call_args.args[0]
    assert original.message_id == payload["messageId"]
    assert original.sender.phone == payload["sender"]["phone"]
    client.respond_order.assert_not_awaited()
    client.respond_inquiry.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "url",
    [
        "https://proyecto.2026-2.tallerdeintegracion.cl/messaging/",
        f"http://{module.DEV_HOST}/messaging/",
        "https://another-host.test/messaging/",
    ],
)
async def test_probe_does_not_send_outside_development(probe, monkeypatch, url):
    path, _, _, factory = probe
    monkeypatch.setattr(
        module, "get_settings", lambda: make_settings(messaging_base_url=url)
    )
    result = await module.run(path, text="Prueba.")
    assert result["error"] == "development_messaging_url_required"
    factory.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("age", [timedelta(minutes=31), timedelta(minutes=-1)])
async def test_probe_requires_a_recent_message(probe, age):
    path, payload, _, factory = probe
    payload["receivedAt"] = (datetime.now(UTC) - age).isoformat()
    path.write_text(json.dumps(payload))
    result = await module.run(path, text="Prueba.")
    assert result["error"] == "message_outside_response_window"
    factory.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize("content", ["not JSON", "{}", "null"])
async def test_probe_rejects_an_invalid_webhook_without_sending(probe, content):
    path, _, _, factory = probe
    path.write_text(content)
    result = await module.run(path, text="Prueba.")
    assert result["error"] == "invalid_webhook_file"
    factory.assert_not_called()


@pytest.mark.anyio
async def test_probe_rejects_blank_text(probe):
    path, _, _, factory = probe
    result = await module.run(path, text=" ")
    assert result["error"] == "reply_text_required"
    factory.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        MessagingTimeoutError("private-password"),
        MessagingHTTPError(404),
        MessagingHTTPError(500),
    ],
)
async def test_probe_reports_failure_without_retrying_or_exposing_secrets(
    probe, failure
):
    path, _, client, _ = probe
    client.send_reply.side_effect = failure
    result = await module.run(path, text="Prueba.")
    assert result["status"] == "failed"
    assert result["error"] == type(failure).__name__
    assert result["http_status"] == getattr(failure, "status_code", None)
    assert "private-password" not in str(result)
    client.send_reply.assert_awaited_once()


def test_cli_returns_failure_exit_code_for_a_missing_webhook(probe, capsys):
    path, _, _, factory = probe
    assert (
        module.main(
            ["--message-file", str(path.with_name("missing.json")), "--text", "Prueba."]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "invalid_webhook_file"
    factory.assert_not_called()
