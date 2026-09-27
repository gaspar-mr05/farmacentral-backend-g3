from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FarmaCentralProduction(BaseModel):
    batch: int = Field(gt=0)
    at: Literal["farma-central", "packaging"]


class FarmaCentralStorage(BaseModel):
    cold: bool = False


class FarmaCentralProduct(BaseModel):
    sku: str = Field(min_length=1)
    name: str = Field(min_length=1)
    production: FarmaCentralProduction
    sellable: bool
    storage: FarmaCentralStorage = Field(default_factory=FarmaCentralStorage)


class FarmaCentralSpace(BaseModel):
    external_id: str = Field(alias="_id", min_length=1)
    buffer: bool = False
    packaging: bool = False
    check_in: bool = Field(default=False, alias="checkIn")
    check_out: bool = Field(default=False, alias="checkOut")
    quarantine: bool = False
    cold: bool = False


class FarmaCentralInventoryItem(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=0)


class FarmaCentralUnit(BaseModel):
    external_id: str = Field(alias="_id", min_length=1)
    sku: str = Field(min_length=1)
    store_id: str = Field(alias="store", min_length=1)
    expires_at: datetime = Field(alias="expiresAt")
    batch: str | None = Field(default=None, min_length=1)


class FarmaCentralChallengeRequest(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1, le=5000)


class FarmaCentralChallengeResponse(BaseModel):
    challenge_id: str = Field(alias="challengeId", min_length=1)
    prefix: str = Field(min_length=1)
    algorithm: Literal["sha256-leading-zero-bits"]
    difficulty: int = Field(ge=0)
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1, le=5000)
    expires_at: datetime = Field(alias="expiresAt")


class FarmaCentralProductRequest(BaseModel):
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1, le=5000)
    challenge_id: str = Field(alias="challengeId", min_length=1)
    nonce: str = Field(min_length=1)


class FarmaCentralSupplyResponse(BaseModel):
    sku: str = Field(min_length=1)
    group: int
    quantity: int = Field(ge=1, le=5000)
    available_at: datetime = Field(alias="availableAt")
