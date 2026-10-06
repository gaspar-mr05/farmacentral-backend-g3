from typing import Any

from app.core.config import Settings


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://unused:unused@localhost/unused",
        "farma_central_base_url": "https://example.test/api/",
        "farma_central_api_secret": "test-secret",
        "farma_central_ftp": "test-ftp",
        "farma_central_group": 3,
    }
    values.update(overrides)
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        **values,
    )
