from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import require_messaging_oidc
from app.db.session import get_session
from app.schemas.messaging import MessagingWebhookPayload
from app.services.messaging import MessagingWebhookService

router = APIRouter(tags=["messaging"])


@router.post(
    "/messaging",
    status_code=204,
    dependencies=[Depends(require_messaging_oidc)],
)
def receive_message(
    message: MessagingWebhookPayload,
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    try:
        MessagingWebhookService(session).receive(message)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Message reception could not be persisted",
        ) from exc
    return Response(status_code=204)
