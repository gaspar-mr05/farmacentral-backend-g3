"""Send one reply to an authentic development webhook, without using the LLM."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.clients.messaging import MessagingClient, MessagingError, MessagingHTTPError
from app.core.config import get_settings
from app.schemas.messaging import MessagingWebhookPayload

DEV_HOST = "dev.proyecto.2026-2.tallerdeintegracion.cl"


async def run(message_file: Path, *, text: str) -> dict:
    settings = get_settings()
    report = {
        "scope": "messaging_development_reply",
        "status": "failed",
        "llm_calls": 0,
        "chat_delivery_verified": False,
    }
    url = urlsplit(settings.effective_messaging_base_url)
    if url.scheme != "https" or url.hostname != DEV_HOST:
        return report | {"error": "development_messaging_url_required"}
    try:
        message = MessagingWebhookPayload.model_validate_json(message_file.read_text())
    except (OSError, ValidationError):
        return report | {"error": "invalid_webhook_file"}
    if not text.strip():
        return report | {"error": "reply_text_required"}
    age = datetime.now(UTC) - message.received_at
    if not timedelta(0) <= age < timedelta(minutes=30):
        return report | {"error": "message_outside_response_window"}

    report["reply_to"] = message.message_id
    try:
        async with MessagingClient(settings=settings) as client:
            confirmation = await client.send_reply(message, text=text)
    except MessagingError as exc:
        return report | {
            "error": type(exc).__name__,
            "http_status": getattr(exc, "status_code", None),
            "outcome_uncertain": (
                not isinstance(exc, MessagingHTTPError) or exc.status_code >= 500
            ),
        }
    # An HTTP acknowledgement is not proof that the text appeared in the chat.
    # Report field names only: the evolving response may contain private data.
    return report | {
        "status": "accepted",
        "confirmation_fields": sorted(confirmation),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-file", type=Path, required=True)
    parser.add_argument("--text", required=True)
    args = parser.parse_args(argv)
    report = asyncio.run(run(args.message_file, text=args.text))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] != "accepted")


if __name__ == "__main__":
    raise SystemExit(main())
