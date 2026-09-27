from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Product, ProductCategory


def test_list_products_returns_public_product_data(
    api_client: TestClient,
    db_session: Session,
) -> None:
    sku = f"API-PRODUCT-{uuid4().hex}"
    product = Product(
        sku=sku,
        name="Producto API",
        category=ProductCategory.INSUMO,
        batch_size=25,
        requires_refrigeration=True,
    )
    db_session.add(product)
    db_session.flush()

    response = api_client.get("/api/products")

    assert response.status_code == 200
    matching_products = [item for item in response.json() if item["sku"] == sku]
    assert matching_products == [
        {
            "sku": sku,
            "name": "Producto API",
            "category": "insumo",
            "batch_size": 25,
            "requires_refrigeration": True,
        }
    ]
