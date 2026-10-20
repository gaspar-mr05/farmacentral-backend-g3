from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin

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
    purchase_orders_base_url: AnyHttpUrl | None = None
    purchase_orders_timeout_seconds: float = Field(default=10, gt=0)
    sftp_host: str = Field(default="choripan10.ing.uc.cl", min_length=1)
    sftp_port: int = Field(default=22, ge=1, le=65535)
    sftp_username: str | None = Field(default=None, min_length=1)
    sftp_password: SecretStr | None = Field(default=None, min_length=1)
    sftp_directory: str = Field(default=".", min_length=1)
    sftp_known_hosts_file: Path | None = None
    sftp_timeout_seconds: float = Field(default=10, gt=0)
    sftp_poll_interval_seconds: float = Field(default=30, gt=0)
    sftp_max_file_bytes: int = Field(default=65536, gt=0)
    sftp_batch_size: int = Field(default=100, ge=1)
    market_price_cache_ttl_seconds: float = Field(default=30, ge=0)
    market_price_max_retry_wait_seconds: float = Field(default=2, ge=0)
    checkout_base_url: AnyHttpUrl = AnyHttpUrl(
        "https://dev.proyecto.2026-2.tallerdeintegracion.cl/"
    )
    app_public_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000/")
    frontend_public_url: AnyHttpUrl | None = None
    cors_origins: str = ""

    @property
    def effective_purchase_orders_base_url(self) -> str:
        return str(
            self.purchase_orders_base_url
            or urljoin(str(self.farma_central_base_url), "/ordenes-compra/")
        )

    @property
    def effective_sftp_username(self) -> str:
        if self.sftp_username:
            return self.sftp_username
        host = self.farma_central_base_url.host or ""
        if not host.endswith(".tallerdeintegracion.cl"):
            raise ValueError("Set SFTP_USERNAME explicitly for a custom API host")
        environment = "desarrollo" if host.startswith("dev.") else "produccion"
        return f"grupo{self.farma_central_group}_{environment}"

    @property
    def effective_sftp_password(self) -> SecretStr:
        return self.sftp_password or SecretStr(self.farma_central_ftp)

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
