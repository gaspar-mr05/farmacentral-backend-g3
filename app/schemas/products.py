from dataclasses import dataclass

from pydantic import BaseModel

from app.models import ProductCategory


@dataclass(frozen=True)
class ProductData:
    sku: str
    name: str
    category: ProductCategory
    batch_size: int
    requires_refrigeration: bool


class ProductResponse(BaseModel):
    sku: str
    name: str
    category: ProductCategory
    batch_size: int
    requires_refrigeration: bool
