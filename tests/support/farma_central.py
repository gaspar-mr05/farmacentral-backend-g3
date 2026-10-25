from typing import Any

from app.core.config import Settings


def catalog_product(sku, name, *, sellable=False, components=()):
    return {
        "sku": sku,
        "name": name,
        "sellable": sellable,
        "production": {
            "at": "packaging" if sellable or components else "farma-central",
            "batch": 1,
        },
        "components": [{"sku": component, "req": 1} for component in components],
    }


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://unused:unused@localhost/unused",
        "farma_central_base_url": "https://example.test/api/",
        "farma_central_api_secret": "test-secret",
        "farma_central_ftp": "test-ftp",
        "farma_central_group": 3,
    }
    values.update(overrides)
    return Settings(
        _env_file=None,
        **values,
    )
