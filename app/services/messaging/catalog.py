"""Compact clinical kit context from the official recipes."""

from pydantic import TypeAdapter, ValidationError

from app.clients.farma_central import FarmaCentralInvalidResponseError, JSONResponse
from app.schemas.farma_central import FarmaCentralProduct
from app.schemas.llm_gateway import LLMCatalogItem


def kit_catalog(payload: JSONResponse, clinical_skus: set[str]) -> list[LLMCatalogItem]:
    try:
        products = TypeAdapter(list[FarmaCentralProduct]).validate_python(payload)
    except ValidationError as exc:
        raise FarmaCentralInvalidResponseError("Invalid messaging catalog") from exc
    by_sku = {product.sku: product for product in products}
    if not products or len(by_sku) != len(products):
        raise FarmaCentralInvalidResponseError("Empty or duplicated messaging catalog")

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
            names.add(component.name)
            pending.extend(item.sku for item in component.components)
        catalog.append(
            LLMCatalogItem(sku=kit.sku, name=kit.name, components=tuple(sorted(names)))
        )
    return catalog
