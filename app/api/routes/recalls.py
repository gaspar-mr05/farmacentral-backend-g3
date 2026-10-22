from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recall_oidc
from app.db.session import get_session
from app.schemas.recalls import RecallWebhookPayload
from app.services.recalls import RecallWebhookService

router = APIRouter(tags=["recalls"])


@router.post(
    "/recalls",
    status_code=204,
    dependencies=[Depends(require_recall_oidc)],
)
def receive_recall(
    recall: RecallWebhookPayload,
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    try:
        RecallWebhookService(session).receive(recall)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Recall reception could not be persisted",
        ) from exc
    return Response(status_code=204)
