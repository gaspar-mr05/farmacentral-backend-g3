from typing import Annotated

from pydantic import BaseModel, StringConstraints

StoreId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProductMovementRequest(BaseModel):
    store: StoreId


class ProductMovementResponse(BaseModel):
    product_id: str
    from_store: str
    to_store: str
    moved: bool
