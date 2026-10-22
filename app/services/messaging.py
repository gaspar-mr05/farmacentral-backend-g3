import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.inbound_events import record_messaging_message
from app.schemas.messaging import MessagingWebhookPayload

logger = logging.getLogger(__name__)


class MessagingWebhookService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def receive(self, message: MessagingWebhookPayload) -> None:
        try:
            # Bound waits on duplicate inserts and busy database statements.
            self._session.execute(select(func.set_config("lock_timeout", "3000", True)))
            self._session.execute(
                select(func.set_config("statement_timeout", "5000", True))
            )
            created = record_messaging_message(self._session, message)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        logger.info(
            "Messaging webhook received: message_id=%s duplicate=%s",
            message.message_id,
            not created,
        )
