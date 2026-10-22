import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.inbound_events import record_recall
from app.schemas.recalls import RecallWebhookPayload

logger = logging.getLogger(__name__)


class RecallWebhookService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def receive(self, recall: RecallWebhookPayload) -> None:
        try:
            self._session.execute(select(func.set_config("lock_timeout", "3000", True)))
            self._session.execute(
                select(func.set_config("statement_timeout", "5000", True))
            )
            created = record_recall(self._session, recall)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        logger.info(
            "Recall webhook received: recall_id=%s duplicate=%s",
            recall.recall_id,
            not created,
        )
