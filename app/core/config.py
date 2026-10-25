from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
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
    pubsub_project_id: str = Field(default="taller-integracion-310700", min_length=1)
    pubsub_topic_id: str | None = Field(default=None, min_length=1)
    pubsub_subscription_id: str | None = Field(default=None, min_length=1)
    pubsub_credentials_file: Path | None = None
    pubsub_timeout_seconds: float = Field(default=10, gt=0)
    pubsub_poll_interval_seconds: float = Field(default=5, gt=0)
    pubsub_batch_size: int = Field(default=100, ge=1)
    external_order_poll_interval_seconds: float = Field(default=5, gt=0)
    external_order_batch_size: int = Field(default=100, ge=1)
    replenishment_poll_interval_seconds: float = Field(default=60, gt=0)
    replenishment_minimum_stock: int = Field(default=2, ge=1)
    replenishment_target_stock: int = Field(default=6, ge=1)
    replenishment_minimum_lifetime_seconds: float = Field(default=14400, ge=0)
    messaging_oidc_audience: AnyHttpUrl | None = None
    messaging_oidc_service_account: str = Field(
        default="mensajeria@taller-integracion-310700.iam.gserviceaccount.com",
        min_length=1,
    )
    messaging_base_url: AnyHttpUrl | None = None
    messaging_timeout_seconds: float = Field(default=10, gt=0)
    messaging_poll_interval_seconds: float = Field(default=12, gt=0)
    messaging_batch_size: int = Field(default=1, ge=1)
    recall_oidc_audience: AnyHttpUrl | None = None
    recall_oidc_service_account: str = Field(
        default="mensajeria@taller-integracion-310700.iam.gserviceaccount.com",
        min_length=1,
    )
    recall_poll_interval_seconds: float = Field(default=30, gt=0)
    recall_batch_size: int = Field(default=20, ge=1)
    oidc_timeout_seconds: float = Field(default=3, gt=0, le=5)
    llm_gateway_base_url: AnyHttpUrl | None = None
    llm_gateway_api_key: SecretStr | None = Field(default=None, min_length=1)
    llm_gateway_model: str | None = Field(default=None, min_length=1, pattern=r"\S")
    llm_gateway_timeout_seconds: float = Field(default=20, gt=0)
    llm_gateway_max_tokens: int = Field(default=256, gt=0)
    market_price_cache_ttl_seconds: float = Field(default=30, ge=0)
    market_price_max_retry_wait_seconds: float = Field(default=2, ge=0)
    checkout_base_url: AnyHttpUrl = AnyHttpUrl(
        "https://dev.proyecto.2026-2.tallerdeintegracion.cl/"
    )
    app_public_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8000/")
    frontend_public_url: AnyHttpUrl | None = None
    cors_origins: str = ""

    @model_validator(mode="after")
    def validate_replenishment_stock(self) -> "Settings":
        if self.replenishment_target_stock < self.replenishment_minimum_stock:
            raise ValueError(
                "REPLENISHMENT_TARGET_STOCK must be >= REPLENISHMENT_MINIMUM_STOCK"
            )
        return self

    @property
    def effective_purchase_orders_base_url(self) -> str:
        return str(
            self.purchase_orders_base_url
            or urljoin(str(self.farma_central_base_url), "/ordenes-compra/")
        )

    @property
    def effective_messaging_oidc_audience(self) -> str:
        return str(
            self.messaging_oidc_audience
            or urljoin(str(self.app_public_url), "/api/messaging")
        )

    @property
    def effective_messaging_base_url(self) -> str:
        return str(
            self.messaging_base_url
            or urljoin(str(self.farma_central_base_url), "/messaging/")
        )

    @property
    def effective_recall_oidc_audience(self) -> str:
        return str(
            self.recall_oidc_audience
            or urljoin(str(self.app_public_url), "/api/recalls")
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
    def effective_pubsub_topic_id(self) -> str:
        if self.pubsub_topic_id:
            return self.pubsub_topic_id
        return (
            f"{self._farma_central_environment}.proyecto.2026-2."
            f"grupo{self.farma_central_group}"
        )

    @property
    def effective_pubsub_subscription_id(self) -> str:
        return self.pubsub_subscription_id or (
            f"grupo{self.farma_central_group}-{self._farma_central_environment}"
        )

    @property
    def _farma_central_environment(self) -> str:
        host = self.farma_central_base_url.host or ""
        if host.startswith("dev."):
            return "dev"
        if host.endswith(".tallerdeintegracion.cl"):
            return "prod"
        raise ValueError(
            "Set PUBSUB_TOPIC_ID and PUBSUB_SUBSCRIPTION_ID for a custom API host"
        )

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
