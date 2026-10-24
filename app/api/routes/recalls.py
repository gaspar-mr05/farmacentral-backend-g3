from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import require_recall_oidc
from app.db.session import get_session
from app.schemas.recalls import (
    RecallDetailResponse,
    RecallSummaryResponse,
    RecallWebhookPayload,
)
from app.services.recalls import (
    RecallNotFoundError,
    RecallQueryService,
    RecallWebhookService,
)

router = APIRouter(tags=["recalls"])


@router.get("/recalls", response_model=list[RecallSummaryResponse])
def list_recalls(
    session: Annotated[Session, Depends(get_session)],
) -> list[RecallSummaryResponse]:
    return RecallQueryService(session).list_recalls()


@router.get("/recalls/{recall_id}", response_model=RecallDetailResponse)
def get_recall(
    recall_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> RecallDetailResponse:
    try:
        return RecallQueryService(session).get_recall(recall_id)
    except RecallNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
