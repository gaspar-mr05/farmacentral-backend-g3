"""Clinical kits with only recipe names relevant to the incoming message."""

import re
import unicodedata

from pydantic import TypeAdapter, ValidationError

from app.clients.farma_central import FarmaCentralInvalidResponseError, JSONResponse
from app.schemas.farma_central import FarmaCentralProduct
from app.schemas.llm_gateway import LLMCatalogItem


def _keywords(text: str) -> set[str]:
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFD", text.casefold())
        if not unicodedata.combining(char)
    )
    return {
        word.rstrip("s")
        for word in re.findall(r"[a-z0-9]+", normalized)
        if len(word) > 3
    }


def kit_catalog(
    payload: JSONResponse, clinical_skus: set[str], *, message_text: str
) -> list[LLMCatalogItem]:
    try:
        products = TypeAdapter(list[FarmaCentralProduct]).validate_python(payload)
    except ValidationError as exc:
        raise FarmaCentralInvalidResponseError("Invalid messaging catalog") from exc
    by_sku = {product.sku: product for product in products}
    if not products or len(by_sku) != len(products):
        raise FarmaCentralInvalidResponseError("Empty or duplicated messaging catalog")

    keywords = _keywords(message_text)
    catalog = []
    for sku in sorted(clinical_skus & by_sku.keys()):
        kit = by_sku[sku]
        if not kit.sellable:
            continue
        names = set()
        visited = {sku}
        pending = [component.sku for component in kit.components]
        while pending:
            component_sku = pending.pop()
            if component_sku in visited:
                continue
            visited.add(component_sku)
            component = by_sku.get(component_sku)
            if component is None:
                raise FarmaCentralInvalidResponseError(
                    "Messaging recipe references a missing component"
                )
            if _keywords(component.name) & keywords:
                names.add(component.name)
            pending.extend(item.sku for item in component.components)
        catalog.append(
            LLMCatalogItem(sku=kit.sku, name=kit.name, components=tuple(sorted(names)))
        )
    return catalog
