import re
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    BusinessPeriod,
    InboundEvent,
    MessageDirection,
    MessagingMessage,
)
from app.schemas.messaging import MessagingWebhookPayload


def normalize_phone(value: str) -> str:
    """Canonicalize common Chilean and international phone representations."""
    digits = re.sub(r"\D", "", value)
    if value.strip().startswith("00"):
        digits = digits[2:]
    elif len(digits) == 9 and digits.startswith("9"):
        digits = f"56{digits}"
    if not 8 <= len(digits) <= 15:
        raise ValueError("Phone number must contain between 8 and 15 digits")
    return f"+{digits}"


def messaging_environment(settings: Settings) -> str:
    host = urlsplit(str(settings.farma_central_base_url)).hostname or ""
    if host.startswith("dev."):
        return "dev"
    if host.startswith("prod."):
        return "prod"
    return settings.app_env


def period_for_message(
    session: Session,
    *,
    group_id: int,
    environment: str,
    occurred_at: datetime,
) -> BusinessPeriod | None:
    return session.scalar(
        select(BusinessPeriod)
        .where(
            BusinessPeriod.group_id == group_id,
            BusinessPeriod.environment == environment,
            BusinessPeriod.starts_at <= occurred_at,
            or_(
                BusinessPeriod.ends_at.is_(None),
                BusinessPeriod.ends_at > occurred_at,
            ),
        )
        .order_by(BusinessPeriod.starts_at.desc())
        .limit(1)
    )


def add_inbound_message(
    session: Session,
    event: InboundEvent,
    message: MessagingWebhookPayload,
    settings: Settings,
) -> MessagingMessage:
    environment = messaging_environment(settings)
    period = period_for_message(
        session,
        group_id=settings.farma_central_group,
        environment=environment,
        occurred_at=message.received_at,
    )
    row = MessagingMessage(
        inbound_event=event,
        direction=MessageDirection.INBOUND,
        external_message_id=message.message_id,
        phone_normalized=normalize_phone(message.sender.phone),
        sender=message.sender.model_dump(mode="json"),
        text=message.text,
        payload=message.model_dump(mode="json", by_alias=True),
        occurred_at=message.received_at,
        group_id=settings.farma_central_group,
        environment=environment,
        period_id=period.id if period else None,
    )
    session.add(row)
    return row


def add_outbound_message(
    session: Session,
    event: InboundEvent,
    *,
    payload: dict,
    text: str | None = None,
    occurred_at: datetime,
) -> MessagingMessage | None:
    incoming = session.scalar(
        select(MessagingMessage).where(
            MessagingMessage.inbound_event_id == event.id,
            MessagingMessage.direction == MessageDirection.INBOUND,
        )
    )
    if incoming is None:
        return None
    row = MessagingMessage(
        inbound_event=event,
        direction=MessageDirection.OUTBOUND,
        phone_normalized=incoming.phone_normalized,
        sender={"group": incoming.group_id},
        text=text,
        payload=payload,
        occurred_at=occurred_at,
        group_id=incoming.group_id,
        environment=incoming.environment,
        period_id=incoming.period_id,
    )
    session.add(row)
    return row


def conversation_history(
    session: Session,
    *,
    phone: str,
    group_id: int,
    environment: str,
    period_id=None,
) -> list[MessagingMessage]:
    """Return one customer's history without crossing an operational scope."""
    period_filter = (
        MessagingMessage.period_id.is_(None)
        if period_id is None
        else MessagingMessage.period_id == period_id
    )
    return list(
        session.scalars(
            select(MessagingMessage)
            .where(
                MessagingMessage.phone_normalized == normalize_phone(phone),
                MessagingMessage.group_id == group_id,
                MessagingMessage.environment == environment,
                period_filter,
            )
            .order_by(MessagingMessage.occurred_at, MessagingMessage.id)
        )
    )
