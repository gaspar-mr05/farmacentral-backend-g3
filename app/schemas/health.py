from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(HealthResponse):
    database: str
