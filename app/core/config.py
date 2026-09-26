from functools import lru_cache

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Farmacentral Backend"
    app_env: str = "local"
    database_url: str = Field(min_length=1)
    farma_central_base_url: AnyHttpUrl
    farma_central_api_secret: SecretStr
    farma_central_ftp: str = Field(min_length=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
