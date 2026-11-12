from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

NonBlankString = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class LLMCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sku: NonBlankString
    name: NonBlankString
    components: tuple[NonBlankString, ...] = ()


class LLMOrderProposal(BaseModel):
    """Untrusted proposal: still requires deterministic business validation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    intent: Literal["order", "inquiry", "discarded"]
    order_reference: NonBlankString | None
    sku: NonBlankString | None
    quantity: Annotated[int, Field(gt=0)] | None
    delivery_date: AwareDatetime | None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    is_ambiguous: bool
    reason: str
