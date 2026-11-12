"""Durable inquiry classification and text delivery, without blind POST retries."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.clients.messaging import MessagingClient, MessagingError, MessagingHTTPError
from app.db.messaging_messages import add_outbound_message
from app.models import InboundEvent
from app.schemas.messaging import MessagingWebhookPayload

from .inquiry import InquiryResolutionService


class InquiryDeliveryError(Exception):
    """A terminal or uncertain delivery requires review rather than another POST."""


class InquiryFlowService:
    def __init__(
        self,
        messaging: MessagingClient,
        resolver: InquiryResolutionService,
        session: Session,
    ) -> None:
        self._messaging = messaging
        self._resolver = resolver
        self._session = session

    async def process(self, event: InboundEvent) -> None:
        try:
            message = MessagingWebhookPayload.model_validate(event.payload)
        except ValidationError as exc:
            raise InquiryDeliveryError("Invalid inquiry webhook") from exc
        deadline = message.received_at + timedelta(minutes=30)
        evidence = event.resolution_evidence or {}
        delivery = deepcopy(evidence.get("delivery"))
        if delivery is None:
            if event.responded_at is not None:
                raise InquiryDeliveryError(
                    "Legacy response does not confirm E3 delivery"
                )
            text = await self._resolver.resolve(event)
            delivery = {
                "preparedAt": datetime.now(UTC).isoformat(),
                "classification": {
                    "state": "pending",
                    "kind": "inquiry"
                    if event.consulted_external_order_id
                    else "discarded",
                    "orderId": event.consulted_external_order_id,
                },
                "reply": {
                    "state": "pending",
                    "to": message.sender.phone,
                    "replyTo": message.message_id,
                    "text": text,
                },
            }
            self._save(event, delivery)

        reply = delivery["reply"]
        if (
            reply["to"] != message.sender.phone
            or reply["replyTo"] != message.message_id
        ):
            raise InquiryDeliveryError("Saved reply does not match the original query")
        for name in ("classification", "reply"):
            phase = delivery[name]
            if phase["state"] == "confirmed":
                continue
            if phase["state"] in {"sending", "uncertain"}:
                phase["state"] = "uncertain"
                self._save(event, delivery)
                raise InquiryDeliveryError(
                    f"Inquiry {name} outcome is uncertain; reconcile before retrying"
                )
            if phase["state"] != "pending":
                raise InquiryDeliveryError("Invalid saved inquiry delivery phase")
            if datetime.now(UTC) >= deadline:
                raise InquiryDeliveryError(
                    f"Inquiry response window expired before {name}"
                )

            phase.update(
                state="sending",
                attemptedAt=datetime.now(UTC).isoformat(),
                attempts=phase.get("attempts", 0) + 1,
            )
            self._save(event, delivery)
            try:
                if name == "reply":
                    confirmation = await self._messaging.send_reply(
                        message, text=reply["text"]
                    )
                elif phase["kind"] == "inquiry":
                    confirmation = await self._messaging.respond_inquiry(
                        message.message_id, order_id=phase["orderId"]
                    )
                else:
                    # A reference that cannot be resolved cannot name a guessed OC.
                    await self._messaging.discard(
                        message.message_id, reason=reply["text"]
                    )
                    confirmation = {}
            except MessagingError as exc:
                uncertain = (
                    not isinstance(exc, MessagingHTTPError)
                    or exc.status_code == 409
                    or exc.status_code >= 500
                )
                phase.update(
                    state="uncertain" if uncertain else "pending",
                    lastError=type(exc).__name__,
                    lastHttpStatus=getattr(exc, "status_code", None),
                )
                self._save(event, delivery)
                if uncertain:
                    raise InquiryDeliveryError(
                        f"Inquiry {name} outcome is uncertain; "
                        "reconcile before retrying"
                    ) from exc
                raise

            confirmed_at = datetime.now(UTC)
            phase.update(
                state="confirmed",
                confirmedAt=confirmed_at.isoformat(),
                confirmation=confirmation,
                latencySeconds=(confirmed_at - message.received_at).total_seconds(),
            )
            if name == "reply":
                add_outbound_message(
                    self._session,
                    event,
                    payload={
                        "to": reply["to"],
                        "replyTo": reply["replyTo"],
                        "text": reply["text"],
                        "confirmation": confirmation,
                    },
                    text=reply["text"],
                    occurred_at=confirmed_at,
                )
                if confirmed_at < deadline:
                    event.responded_at = confirmed_at
            self._save(event, delivery)

        if any(
            datetime.fromisoformat(delivery[name]["confirmedAt"]) >= deadline
            for name in ("classification", "reply")
        ):
            raise InquiryDeliveryError(
                "Inquiry confirmation arrived after its deadline"
            )
        event.responded_at = datetime.fromisoformat(reply["confirmedAt"])

    def _save(self, event: InboundEvent, delivery: dict) -> None:
        event.resolution_evidence = {
            **(event.resolution_evidence or {}),
            "delivery": deepcopy(delivery),
        }
        self._session.commit()
