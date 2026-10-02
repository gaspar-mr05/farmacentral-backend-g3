from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_session
from app.schemas.traceability import TraceabilityResponse
from app.services.traceability import LotNotFoundError, TraceabilityService

router = APIRouter(tags=["traceability"])


@router.get(
    "/traceability/{lot_id}",
    response_model=TraceabilityResponse,
)
def get_traceability(
    lot_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> TraceabilityResponse:
    try:
        return TraceabilityService(session).get_traceability(lot_id)
    except LotNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
