from functools import lru_cache

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Farmacentral Backend"
    app_env: str = "local"
    database_url: str = Field(min_length=1)
    farma_central_base_url: AnyHttpUrl
    farma_central_api_secret: SecretStr
    farma_central_ftp: str = Field(min_length=1)
    farma_central_group: int = Field(gt=0)
    farma_central_timeout_seconds: float = Field(default=10, gt=0)
    market_price_cache_ttl_seconds: float = Field(default=30, ge=0)
    market_price_max_retry_wait_seconds: float = Field(default=2, ge=0)
    checkout_base_url: AnyHttpUrl = AnyHttpUrl(
        "https://dev.proyecto.2026-2.tallerdeintegracion.cl/"
    )
    app_public_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000/")
    frontend_public_url: AnyHttpUrl | None = None
    cors_origins: str = ""

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
