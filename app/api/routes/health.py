from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.db.health import database_available
from app.schemas.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def readiness(
    response: Response,
    available: Annotated[bool, Depends(database_available)],
) -> ReadinessResponse:
    response.headers["Cache-Control"] = "no-store"
    if not available:
        response.status_code = 503
    return ReadinessResponse(
        status="ok" if available else "unavailable",
        database="ok" if available else "unavailable",
    )
